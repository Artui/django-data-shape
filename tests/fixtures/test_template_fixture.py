"""A test reaching a finished world: the test database replaced by a clone of a template."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from django.db import connections

from django_data_shape import (
    Constant,
    Shape,
    Table,
    derive_template,
    drop_database,
    template_database,
)
from django_data_shape.fixtures import template_fixture
from django_data_shape.utils import FINISHED_TEMPLATE
from tests.testapp.models import Catalogue, Company

# Its own alias, with a database of its own: replacing the default test database
# would replace it under every other test in the session, the session world in
# test_fixtures among them. See tests/conftest_settings.py.
ALIAS = "templated"

pytestmark = pytest.mark.django_db(databases=[ALIAS])

CATALOGUE = Catalogue._meta.db_table
COMPANY = Company._meta.db_table

_SHAPE = Shape(Table(Catalogue, rows=60, name=Constant("placeholder")), seed=17)

# The templates the fixture made, so the module can drop them: a template is a
# cache on the machine and nothing removes one on its own.
_made: list[str] = []


def _replay(using: str) -> None:
    """The application's own code: a declared column rewritten, an undeclared table filled."""
    with connections[using].cursor() as cursor:
        cursor.execute(
            f"UPDATE {CATALOGUE} SET name = CASE WHEN id % 4 = 0 THEN 'cog' ELSE 'widget' END"
        )
        cursor.execute(f"INSERT INTO {COMPANY} (name) SELECT 'acme' FROM generate_series(1, 300)")


def _finished_world(using: str) -> str:
    """What a project's conftest writes: build the shape, then derive the finished world."""
    template = template_database(_SHAPE, using=using)
    _made.append(template)
    finished = derive_template(template, _replay, key="replay-1", using=using)
    _made.append(finished)
    # On a warm machine both calls above are hits, which touch only a nodb
    # cursor and leave the connection pytest-django opened attached to the test
    # database; on this cold run the derive's miss closed it. Reopened so the
    # fixture meets the warm state, where dropping the test database fails
    # unless the fixture closes it first.
    connections[using].ensure_connection()
    return finished


# Exactly what a project writes in its own conftest.py.
templated = template_fixture(_finished_world, using=ALIAS)


@pytest.fixture(scope="module")
def finished(templated: str, django_db_blocker: Any) -> Iterator[str]:
    yield templated
    with django_db_blocker.unblock():
        for name in reversed(_made):
            drop_database(name, using=ALIAS)


def _names() -> list[tuple[str, int]]:
    with connections[ALIAS].cursor() as cursor:
        cursor.execute(f"SELECT name, count(*) FROM {CATALOGUE} GROUP BY name ORDER BY 1")
        return [(str(name), int(count)) for name, count in cursor.fetchall()]


def test_a_test_reaches_the_world_the_step_finished(finished: str) -> None:
    assert _names() == [("cog", 15), ("widget", 45)]
    assert Company.objects.using(ALIAS).count() == 300


def test_and_the_planner_sees_it_as_the_step_left_it(finished: str) -> None:
    with connections[ALIAS].cursor() as cursor:
        cursor.execute(
            "SELECT most_common_vals::text FROM pg_stats WHERE tablename = %s AND attname = 'name'",
            [CATALOGUE],
        )
        (values,) = cursor.fetchone()
        cursor.execute("SELECT count(*) FROM pg_stats WHERE tablename = %s", [COMPANY])
        (company_statistics,) = cursor.fetchone()

    assert "widget" in values
    assert "placeholder" not in values
    assert company_statistics > 0


def test_the_fixture_hands_back_the_template_it_cloned(finished: str) -> None:
    assert FINISHED_TEMPLATE.fullmatch(finished)
    assert finished == _made[-1]
    # The test database is a clone under pytest-django's own name for it, never
    # the template, which refuses connections.
    assert connections[ALIAS].settings_dict["NAME"] != finished


def test_a_test_may_write_into_the_finished_world(finished: str) -> None:
    Company.objects.using(ALIAS).create(name="written by the test")

    assert Company.objects.using(ALIAS).count() == 301


def test_and_the_next_test_does_not_see_what_it_wrote(finished: str) -> None:
    # The clone is the test database pytest-django set up, so its db fixture
    # wraps each test in a transaction over it as over any other.
    assert Company.objects.using(ALIAS).count() == 300
