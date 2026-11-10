from __future__ import annotations

from types import SimpleNamespace

import pytest

from django_data_shape import (
    BuildResult,
    Constant,
    ScaleProtocol,
    Shape,
    Table,
    scaled_world,
)
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
    # The BuildResult says how big the world is, rather than counting it again.
    assert catalogue.rows == 25


@pytest.mark.django_db
def test_the_world_is_built_once_for_the_whole_session(catalogue: BuildResult) -> None:
    # Identity, and it has to be.
    assert catalogue is _handed_out[0]
    assert Catalogue.objects.count() == 25


@pytest.mark.django_db
def test_a_test_may_write_into_the_session_world(catalogue: BuildResult) -> None:
    Catalogue.objects.create(name="written by the test")

    assert Catalogue.objects.count() == 26


@pytest.mark.django_db
def test_and_the_next_test_does_not_see_what_it_wrote(catalogue: BuildResult) -> None:
    # The world is committed once; each test's own writes roll back with its transaction.
    assert Catalogue.objects.count() == 25


@pytest.fixture
def one_catalogue() -> Catalogue:
    """An ordinary per-test fixture, written by somebody who has not met this package."""
    return Catalogue.objects.create(name="made by an ordinary fixture")


@pytest.mark.django_db
def test_an_ordinary_fixture_over_the_session_world_does_not_start_from_empty(
    catalogue: BuildResult, one_catalogue: Catalogue
) -> None:
    # The session world is committed outside every transaction, so ordinary tests see it.
    assert Catalogue.objects.count() == 26
    assert Catalogue.objects.exclude(pk=one_catalogue.pk).count() == 25


@pytest.mark.django_db
def test_a_scaled_world_builds_over_the_session_world(catalogue: BuildResult) -> None:
    """The composition an application with one model graph needs."""
    world = Shape(Table(Catalogue, rows=5, name=Constant("widget")))

    with scaled_world(world, 1) as rows:
        assert rows == 5
        assert Catalogue.objects.count() == 5

    assert Catalogue.objects.count() == 25


def test_the_scale_fixture_hands_out_one_world_per_factor(companies: ScaleProtocol) -> None:
    # The fixture requests the db fixture, so a test gets access and a transaction.
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
    # The same three things the exception names.
    assert "Building a shape" in reason
    assert "reporting" in reason
    assert "sqlite" in reason
