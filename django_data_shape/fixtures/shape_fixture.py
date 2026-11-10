from __future__ import annotations

from typing import Any

import pytest
from django.db import DEFAULT_DB_ALIAS, connections

from django_data_shape.declaration.shape import Shape
from django_data_shape.fixtures.skip_unless_postgres import skip_unless_postgres
from django_data_shape.loading.build import build
from django_data_shape.types.build_result import BuildResult


def shape_fixture(shape: Shape, *, using: str = DEFAULT_DB_ALIAS) -> object:
    """Session-scoped fixture building ``shape`` once; typed ``object`` as pytest's type varies."""

    @pytest.fixture(scope="session")
    def built_shape(django_db_setup: None, django_db_blocker: Any) -> BuildResult:
        # Reads the gate first: ``vendor`` is a class attribute, so a skip costs no connection.
        skip_unless_postgres(connections[using], "Building a shape for the test session")
        with django_db_blocker.unblock():
            return build(shape, using=using)

    return built_shape
