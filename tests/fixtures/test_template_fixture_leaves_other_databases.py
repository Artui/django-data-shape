from __future__ import annotations

from collections.abc import Iterator
from importlib.metadata import version
from typing import Any

import pytest
from django.db import connections

from django_data_shape import Constant, Shape, Table, drop_database, template_database
from django_data_shape.fixtures import template_fixture
from django_data_shape.fixtures.template_fixture import _is_test_database
from tests.testapp.models import Catalogue

# Configured, never requested: pytest-django makes no test database for it, so
# its NAME is the configured one. See tests/conftest_settings.py. No django_db
# mark in this module for that reason.
ALIAS = "unrequested"

_SHAPE = Shape(Table(Catalogue, rows=5, name=Constant("placeholder")), seed=23)

_made: list[str] = []

# Before 4.11 pytest-django made a test database for every alias, this one
# included, so nothing is left configured for the fixture to protect, and the
# fixture replacing that test database is what it should do.
_EVERY_ALIAS_SET_UP = tuple(int(part) for part in version("pytest-django").split(".")[:2]) < (4, 11)


def _developers_own(using: str) -> str:
    """Make the configured database hold a row of someone's, then name a template."""
    connection = connections[using]
    own = connection.settings_dict["NAME"]
    quote = connection.ops.quote_name
    with connection._nodb_cursor() as cursor:
        cursor.execute(f"DROP DATABASE IF EXISTS {quote(own)}")
        cursor.execute(f"CREATE DATABASE {quote(own)}")
    _made.append(own)
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE precious (id integer)")
        cursor.execute("INSERT INTO precious VALUES (1)")
    template = template_database(_SHAPE, using=using)
    _made.append(template)
    return template


unrequested = template_fixture(_developers_own, using=ALIAS)


@pytest.fixture(scope="module")
def handed(unrequested: str, django_db_blocker: Any) -> Iterator[str]:
    yield unrequested
    with django_db_blocker.unblock():
        connections[ALIAS].close()
        for name in reversed(_made):
            drop_database(name, using=ALIAS)


@pytest.mark.skipif(
    _EVERY_ALIAS_SET_UP, reason="pytest-django before 4.11 makes a test database for every alias"
)
def test_a_database_pytest_django_did_not_make_is_left_alone(
    handed: str, django_db_blocker: Any
) -> None:
    with django_db_blocker.unblock(), connections[ALIAS].cursor() as cursor:
        cursor.execute("SELECT id FROM precious")
        rows = cursor.fetchall()

    assert rows == [(1,)]
    # The template is still built and named, so a test asking for the name gets it.
    assert handed in _made


@pytest.mark.parametrize(
    ("settings_dict", "expected"),
    [
        ({"NAME": "test_shop_gw0", "TEST": {"NAME": "test_shop_gw0"}}, True),
        ({"NAME": "shop", "TEST": {"NAME": "test_shop_gw0"}}, False),
        ({"NAME": "test_shop", "TEST": {}}, True),
        ({"NAME": "shop"}, False),
    ],
    ids=["xdist-made", "xdist-not-made", "made", "not-made"],
)
def test_a_test_database_is_named_as_django_names_one(
    settings_dict: dict[str, Any], expected: bool
) -> None:
    assert _is_test_database(settings_dict) is expected
