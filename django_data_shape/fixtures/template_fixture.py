from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.backends.base.creation import TEST_DATABASE_PREFIX

from django_data_shape.databases.clone_database import clone_database
from django_data_shape.fixtures.skip_unless_postgres import skip_unless_postgres


def template_fixture(
    template: Callable[[str], str],
    *,
    using: str = DEFAULT_DB_ALIAS,
    strategy: str | None = "file_copy",
) -> object:
    """A session-scoped pytest fixture that makes the test database a clone of a template."""

    @pytest.fixture(scope="session")
    def templated_database(django_db_setup: None, django_db_blocker: Any) -> str:
        connection = connections[using]
        skip_unless_postgres(connection, "Cloning a template into the test database")
        with django_db_blocker.unblock():
            name = template(using)
            target = connection.settings_dict["NAME"]
            # Only a database pytest-django made may be replaced, never a developer's own.
            if not _is_test_database(connection.settings_dict):
                return name
            for alias in connections:
                if connections[alias].settings_dict["NAME"] == target:
                    connections[alias].close()
            clone_database(name, target, using=using, strategy=strategy, replace=True)
        return name

    return templated_database


def _is_test_database(settings_dict: dict[str, Any]) -> bool:
    """Whether a connection names the test database Django's test setup gives it."""
    test_name = settings_dict.get("TEST", {}).get("NAME")
    if test_name:
        return settings_dict["NAME"] == test_name
    return str(settings_dict["NAME"]).startswith(TEST_DATABASE_PREFIX)
