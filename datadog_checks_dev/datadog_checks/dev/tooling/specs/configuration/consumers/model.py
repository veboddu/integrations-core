# (C) Datadog, Inc. 2021-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import re

import yaml

from datamodel_code_generator.format import CodeFormatter, PythonVersion
from datamodel_code_generator.parser import LiteralType
from datamodel_code_generator.parser.openapi import OpenAPIParser

from ..utils import default_option_example, sanitize_openapi_object_properties

PYTHON_VERSION = PythonVersion.PY_38
EXTRA_TYPE_FIELDS = ('compact_example', 'default', 'display_default', 'example')

# Singleton allowing `None` to be a valid default value
NO_DEFAULT = object()


def normalize_option_name(option_name):
    return option_name.replace('-', '_')


def get_default_value(option_name, type_data):
    if 'default' in type_data:
        return type_data['default']
    elif 'type' not in type_data or type_data['type'] in ('array', 'object'):
        return NO_DEFAULT

    example = type_data['example']
    if example != default_option_example(option_name):
        return example

    return NO_DEFAULT


def process_model_file(model_file_contents, need_defaults):
    """
    We modify the model files in the following ways:

    - Ensure that every model has a Config with `allow_mutation = False`
    - Convert all `list` types to `collections.abc.Sequence`
    - Convert all `dict` types to `collections.abc.Mapping`
    - Add the necessary imports for default values and custom validators
    """
    lines = model_file_contents.splitlines()

    # TODO: look for builtin types when the Agent upgrades Python to 3.9
    dict_pattern = r'(?<=\b)Dict(?=\b)'
    list_pattern = r'(?<=\b)List(?=\b)'

    import_lines = []
    extra_types_required = set()

    # Keep track of each model
    model_locations = []
    current_model_location = 0

    for i, line in enumerate(lines):
        if line.startswith('from '):
            import_lines.append(i)

        if line.startswith('class '):
            if current_model_location:
                model_locations.append(current_model_location)

            current_model_location = i + 1
        elif current_model_location:
            if not re.match(r' {4}[a-zA-Z0-9_-]+: ', line):
                continue

            field_part, sep, type_part = line.partition(': ')

            new_type_part = re.sub(dict_pattern, 'Mapping', type_part)
            if new_type_part != type_part:
                extra_types_required.add('Mapping')

            final_type_part = re.sub(list_pattern, 'Sequence', new_type_part)
            if final_type_part != new_type_part:
                extra_types_required.add('Sequence')

            lines[i] = f'{field_part}{sep}{final_type_part}'

    if current_model_location:
        model_locations.append(current_model_location)

    # Perform all modifications from this point on in reverse line order
    for model_location in reversed(model_locations):
        config_start_location = model_location + 1
        config_def_line = lines[model_location]

        config_def = '    class Config:'
        if not config_def_line.startswith(config_def):
            lines.insert(model_location, '')
            lines.insert(model_location, config_def)

        lines.insert(config_start_location, '        allow_mutation = False')

    # pydantic imports
    final_import_line = import_lines[-1]
    lines[final_import_line] += ', root_validator, validator'

    local_import_start_location = final_import_line + 1
    for line in reversed(
        (
            '',
            'from datadog_checks.base.utils.functions import identity',
            '',
            'from . import defaults, validators' if need_defaults else 'from . import validators',
        )
    ):
        lines.insert(local_import_start_location, line)

    # TODO: uncomment when the Agent upgrades Python to 3.9
    # if extra_types_required:
    #     imports_part = ', '.join(sorted(extra_types_required))
    #     if len(import_lines) == 2:
    #         lines.insert(final_import_line, f'from collections.abc import {imports_part}')
    #         lines.insert(final_import_line, '')
    #     else:
    #         lines.insert(import_lines[1], f'from collections.abc import {imports_part}')

    # TODO: remove when the Agent upgrades Python to 3.9
    if extra_types_required:
        line_diff = 2
        typing_import_line = final_import_line - line_diff
        line = lines[typing_import_line]
        package_part, sep, imports_part = line.partition(' import ')

        imports = set(imports_part.split(', '))
        for t in ('Dict', 'List'):
            imports.discard(t)

        imports.update(extra_types_required)

        if imports:
            final_imports_part = ', '.join(sorted(imports))
            lines[typing_import_line] = f'{package_part}{sep}{final_imports_part}'
        else:
            for _ in range(line_diff):
                del lines[typing_import_line]

    return lines


class ModelConsumer:
    def __init__(self, spec, code_formatter=None):
        self.spec = spec
        self.code_formatter = code_formatter or self.create_code_formatter()

    def render(self):
        files = {}

        for file in self.spec['files']:
            # (<file name>, (<contents>, <errors>))
            model_files = {}

            root_imports = []
            defaults_file_lines = []
            defaults_file_needs_dynamic_values = False
            defaults_file_needs_value_normalization = False

            for section in sorted(file['options'], key=lambda s: s['name']):
                errors = []

                section_name = section['name']
                if section_name == 'init_config':
                    model_id = 'shared'
                    model_file_name = f'{model_id}.py'
                    schema_name = 'SharedConfig'
                elif section_name == 'instances':
                    model_id = 'instance'
                    model_file_name = f'{model_id}.py'
                    schema_name = 'InstanceConfig'
                # Skip anything checks don't use directly
                else:
                    continue

                root_imports.append(f'from .{model_id} import {schema_name}')

                # We want to create something like:
                #
                # components:
                #   schemas:
                #     InstanceConfig:
                #       required:
                #         - endpoint
                #       properties:
                #         endpoint:
                #           type: string
                #         timeout:
                #           type: number
                #         ...
                openapi_document = {'components': {'schemas': {}}}
                schema = openapi_document['components']['schemas'][schema_name] = {}

                options = schema['properties'] = {}
                required_options = schema['required'] = []
                options_with_defaults = []
                all_options = []

                for option in sorted(section['options'], key=lambda o: o['name']):
                    option_name = option['name']
                    normalized_option_name = normalize_option_name(option_name)

                    if 'value' in option:
                        type_data = option['value']
                    # Some integrations (like `mysql`) have options that are grouped under a top-level option
                    elif 'options' in option:
                        nested_properties = []
                        type_data = {'type': 'object', 'properties': nested_properties}
                        for nested_option in option['options']:
                            nested_type_data = nested_option['value']

                            # Remove fields that aren't part of the OpenAPI specification
                            for extra_field in EXTRA_TYPE_FIELDS:
                                nested_type_data.pop(extra_field, None)

                            nested_properties.append({'name': nested_option['name'], **nested_type_data})
                    else:
                        errors.append(f'Option `{option_name}` must have a `value` or `options` attribute')
                        continue

                    options[option_name] = type_data
                    all_options.append(normalized_option_name)

                    if option['required']:
                        required_options.append(option_name)
                    else:
                        options_with_defaults.append(normalized_option_name)
                        defaults_file_lines.append('')
                        defaults_file_lines.append('')
                        defaults_file_lines.append(f'def {model_id}_{normalized_option_name}(field, value):')

                        default_value = get_default_value(normalized_option_name, type_data)
                        if default_value is not NO_DEFAULT:
                            defaults_file_needs_value_normalization = True
                            defaults_file_lines.append(f'    return make_immutable_check_config({default_value!r})')
                        else:
                            defaults_file_needs_dynamic_values = True
                            defaults_file_lines.append('    return get_default_field_value(field, value)')

                    # Remove fields that aren't part of the OpenAPI specification
                    for extra_field in EXTRA_TYPE_FIELDS:
                        type_data.pop(extra_field, None)

                    sanitize_openapi_object_properties(type_data)

                try:
                    parser = OpenAPIParser(
                        yaml.safe_dump(openapi_document),
                        target_python_version=PythonVersion.PY_38,
                        enum_field_as_literal=LiteralType.All,
                        encoding='utf-8',
                        # TODO: uncomment when the Agent upgrades Python to 3.9
                        # use_standard_collections=True,
                        strip_default_none=True,
                        # https://github.com/koxudaxi/datamodel-code-generator/pull/173
                        field_constraints=True,
                    )
                    model_file_contents = parser.parse()
                except Exception as e:
                    errors.append(f'Error parsing the OpenAPI schema `{schema_name}`: {e}')
                    model_files[model_file_name] = ('', errors)
                    continue

                model_file_lines = process_model_file(model_file_contents, any(options_with_defaults))

                for option_name in options_with_defaults:
                    model_file_lines.append('')
                    model_file_lines.append(f'    @validator({option_name!r}, pre=True, always=True)')
                    model_file_lines.append(f'    def _ensure_default_{option_name}(cls, v, field):')
                    model_file_lines.append(
                        f'        return defaults.{model_id}_{option_name}(field, v) if v is None else v'
                    )

                for option_name in all_options:
                    model_file_lines.append('')
                    model_file_lines.append(f'    @validator({option_name!r}, always=True)')
                    model_file_lines.append(f'    def _validate_{option_name}(cls, v, field):')

                    function_name = f'{model_id}_{option_name}'
                    model_file_lines.append(
                        f'        return getattr(validators, {function_name!r}, identity)(v, field=field)'
                    )

                model_file_lines.append('')
                model_file_lines.append('    @root_validator')
                model_file_lines.append('    def _final_validation(cls, values):')
                model_file_lines.append(f"        return getattr(validators, 'final', identity)(values)")

                model_file_lines.append('')
                if any(len(line) > 120 for line in model_file_lines):
                    model_files[model_file_name] = (
                        self.code_formatter.apply_black('\n'.join(model_file_lines)), errors
                    )
                else:
                    model_files[model_file_name] = ('\n'.join(model_file_lines), errors)

            # Logs-only integrations
            if not model_files:
                continue

            if defaults_file_lines:
                if defaults_file_needs_value_normalization:
                    defaults_file_lines.insert(
                        0, 'from datadog_checks.base.utils.models.utils import make_immutable_check_config'
                    )
                if defaults_file_needs_dynamic_values:
                    defaults_file_lines.insert(
                        0, 'from datadog_checks.base.utils.models.fields import get_default_field_value'
                    )

                defaults_file_lines.append('')
                defaults_file_contents = '\n'.join(defaults_file_lines)
                if defaults_file_needs_value_normalization:
                    defaults_file_contents = self.code_formatter.apply_black(defaults_file_contents)

                model_files['defaults.py'] = (defaults_file_contents, [])

            root_imports.sort()
            root_imports.append('')
            model_files['__init__.py'] = ('\n'.join(root_imports), [])
            model_files['validators.py'] = ('', [])

            files[file['name']] = {file_name: model_files[file_name] for file_name in sorted(model_files)}

        return files

    @staticmethod
    def create_code_formatter():
        return CodeFormatter(PYTHON_VERSION)
