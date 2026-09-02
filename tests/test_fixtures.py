"""The pytest surface, defined the way a consumer defines it and used as one."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from django_data_shape import BuildResult, Constant, ScaleProtocol, Shape, Table
from django_data_shape.fixtures import scale_fixture, shape_fixture, skip_unless_postgres
from tests.testapp.models import Catalogue, Company

# Exactly what a project writes in its own conftest, and the reason these tests
# are worth having at all: the fixtures are the product here, so the suite has
# to run them rather than call the functions behind them.
#
# Catalogue is reserved for the session-scoped world, because that world outlives
# every test after it and any other test building the same table would meet rows
# it did not put there. The scale fixture takes a different model for the same
# reason in reverse: its worlds are undone at the end of each block, so it needs
# a table nobody is holding open.
catalogue = shape_fixture(Shape(Table(Catalogue, rows=25, name=Constant("widget")), seed=3))
companies = scale_fixture(Shape(Table(Company, rows=4, name=Constant("acme")), seed=3))


_handed_out: list[BuildResult] = []


@pytest.mark.django_db
def test_the_session_world_is_there_for_a_test_that_asks_for_it(catalogue: BuildResult) -> None:
    _handed_out.append(catalogue)

    assert Catalogue.objects.count() == 25
    # The BuildResult rather than None, so a test can say how big the world it
    # was handed actually is instead of counting it again.
    assert catalogue.rows == 25


@pytest.mark.django_db
def test_the_world_is_built_once_for_the_whole_session(catalogue: BuildResult) -> None:
    # Identity, and it has to be. A BuildResult is a frozen dataclass, so a
    # rebuild of the same shape hands back one that compares equal, and counting
    # the rows cannot tell the two apart either: a function-scoped fixture would
    # rebuild inside each test's own transaction and find 25 rows here as well,
    # having paid for them again. The object being the same object is the only
    # thing that says generate, COPY and ANALYZE ran once for the session, which
    # is the entire reason this is a fixture rather than a call in each test.
    assert catalogue is _handed_out[0]
    assert Catalogue.objects.count() == 25


@pytest.mark.django_db
def test_a_test_may_write_into_the_session_world(catalogue: BuildResult) -> None:
    Catalogue.objects.create(name="written by the test")

    assert Catalogue.objects.count() == 26


@pytest.mark.django_db
def test_and_the_next_test_does_not_see_what_it_wrote(catalogue: BuildResult) -> None:
    # The composition being claimed, in two tests: the world is committed once
    # outside the per-test transaction, and everything a test does to it is
    # rolled back with that test. Neither half is ours -- the first is
    # django_db_setup plus django_db_blocker, the second is pytest-django's db
    # fixture -- and the pair is what makes one build serve a whole session.
    assert Catalogue.objects.count() == 25


def test_the_scale_fixture_hands_out_one_world_per_factor(companies: ScaleProtocol) -> None:
    # No django_db marker: the fixture requests pytest-django's db fixture, so a
    # test that asks for worlds has database access and an enclosing transaction
    # without having to remember either.
    measured = []
    for factor in (1, 3):
        with companies(factor) as rows:
            measured.append((rows, Company.objects.count()))

    assert measured == [(4, 4), (12, 12)]


def test_the_scale_fixture_leaves_no_world_behind(companies: ScaleProtocol) -> None:
    with companies(2):
        assert Company.objects.count() == 8

    assert Company.objects.count() == 0


# Stub connections rather than a second database, for the reason the backend gate
# is written the way it is: a degradation path reachable only by running the
# suite on the backend it degrades for is a path the coverage gate cannot see.
_POSTGRES = SimpleNamespace(
    vendor="postgresql", alias="default", Database=SimpleNamespace(__name__="psycopg")
)
_SQLITE = SimpleNamespace(
    vendor="sqlite", alias="reporting", Database=SimpleNamespace(__name__="sqlite3")
)


def test_it_says_nothing_where_a_shaped_database_can_exist() -> None:
    skip_unless_postgres(_POSTGRES, "Building a shape")


def test_it_skips_with_the_refusal_itself_as_the_reason() -> None:
    with pytest.raises(pytest.skip.Exception) as raised:
        skip_unless_postgres(_SQLITE, "Building a shape")

    reason = str(raised.value)
    # The same three things the exception names. A skip whose reason is shorter
    # than the refusal would be a suite that reports it skipped something without
    # saying what would have made it run.
    assert "Building a shape" in reason
    assert "reporting" in reason
    assert "sqlite" in reason
