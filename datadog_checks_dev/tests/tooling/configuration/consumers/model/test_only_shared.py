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
          - template: init_config
            options:
            - name: foo
              description: words
              value:
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
        from .shared import SharedConfig
        """
    )

    defaults_contents, defaults_errors = files['defaults.py']
    assert not defaults_errors
    assert defaults_contents == normalize_yaml(
        """
        from datadog_checks.base.utils.models.fields import get_default_field_value


        def shared_foo(field, value):
            return get_default_field_value(field, value)
        """
    )

    shared_model_contents, shared_model_errors = files['shared.py']
    assert not shared_model_errors
    assert shared_model_contents == normalize_yaml(
        """
        from __future__ import annotations

        from typing import Optional

        from pydantic import BaseModel, root_validator, validator

        from datadog_checks.base.utils.functions import identity

        from . import defaults, validators


        class SharedConfig(BaseModel):
            class Config:
                allow_mutation = False

            foo: Optional[str]

            @validator('foo', pre=True, always=True)
            def _ensure_default_foo(cls, v, field):
                return defaults.shared_foo(field, v) if v is None else v

            @validator('foo', always=True)
            def _validate_foo(cls, v, field):
                return getattr(validators, 'shared_foo', identity)(v, field=field)

            @root_validator
            def _final_validation(cls, values):
                return getattr(validators, 'final', identity)(values)
        """
    )
