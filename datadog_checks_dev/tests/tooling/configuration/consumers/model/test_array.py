# (C) Datadog, Inc. 2021-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import pytest

from ...utils import get_model_consumer, normalize_yaml

pytestmark = [pytest.mark.conf, pytest.mark.conf_consumer, pytest.mark.conf_consumer_model]


def test():
    consumer = get_model_consumer(
        """
        name: test
        version: 0.0.0
        files:
        - name: test.yaml
          options:
          - template: instances
            options:
            - name: foo
              required: true
              description: words
              value:
                type: string
            - name: tags
              description: words
              value:
                type: array
                items:
                  type: string
        """
    )

    model_definitions = consumer.render()
    assert len(model_definitions) == 1

    files = model_definitions['test.yaml']
    assert len(files) == 4

    validators_contents, validators_errors = files['validators.py']
    assert not validators_errors
    assert validators_contents == ''

    package_root_contents, package_root_errors = files['__init__.py']
    assert not package_root_errors
    assert package_root_contents == normalize_yaml(
        """
        from .instance import InstanceConfig
        """
    )

    defaults_contents, defaults_errors = files['defaults.py']
    assert not defaults_errors
    assert defaults_contents == normalize_yaml(
        """
        from datadog_checks.base.utils.models.fields import get_default_field_value


        def instance_tags(field, value):
            return get_default_field_value(field, value)
        """
    )

    instance_model_contents, instance_model_errors = files['instance.py']
    assert not instance_model_errors
    assert instance_model_contents == normalize_yaml(
        """
        from __future__ import annotations

        from typing import Optional, Sequence

        from pydantic import BaseModel, root_validator, validator

        from datadog_checks.base.utils.functions import identity

        from . import defaults, validators


        class InstanceConfig(BaseModel):
            class Config:
                allow_mutation = False

            foo: str
            tags: Optional[Sequence[str]]

            @validator('tags', pre=True, always=True)
            def _ensure_default_tags(cls, v, field):
                return defaults.instance_tags(field, v) if v is None else v

            @validator('foo', always=True)
            def _validate_foo(cls, v, field):
                return getattr(validators, 'instance_foo', identity)(v, field=field)

            @validator('tags', always=True)
            def _validate_tags(cls, v, field):
                return getattr(validators, 'instance_tags', identity)(v, field=field)

            @root_validator
            def _final_validation(cls, values):
                return getattr(validators, 'final', identity)(values)
        """
    )
