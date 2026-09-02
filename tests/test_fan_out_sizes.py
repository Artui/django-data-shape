"""Reading a fan-out back: which parents got the children, without an aggregate."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from django.db import connection, connections
from django.db.models import Count

from django_data_shape import (
    Constant,
    FanOut,
    InvalidShape,
    Projection,
    Shape,
    Table,
    WorldChanged,
    Zipf,
    clone_database,
    drop_database,
    fan_out_sizes,
    template_database,
)
from django_data_shape import build as build_shape
from tests.testapp.models import (
    Company,
    Event,
    EventSession,
    OptionalChild,
    Session,
    TemplateSession,
)

_POSTGRES_ONLY = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="COPY loading and planner statistics need PostgreSQL",
)


def _graph(companies: int = 40, sessions: int = 3000, seed: int = 5) -> Shape:
    return Shape(
        Table(Company, rows=companies, name=Constant("acme")),
        Table(
            Session,
            rows=sessions,
            company=FanOut(Zipf(1.2), childless=0.2),
            label=Constant("s"),
        ),
        seed=seed,
    )


def _loaded() -> dict[int, int]:
    """What the child table actually holds, the expensive way, for comparison.

    The ``GROUP BY`` this function exists to spare a consumer. Fine in a test of
    three thousand rows and the whole point at two million: it is the aggregate
    over the entire world that a plan measurement would otherwise have to run
    before it started.
    """
    return dict(
        Session.objects.values("company_id")
        .annotate(loaded=Count("pk"))
        .values_list("company_id", "loaded")
    )


# The three refusals happen before the connection is reached, so they need no
# database at all -- which is also the claim: a declaration this function cannot
# answer for is refused by reading the declaration, not by querying and finding
# nothing.
def test_a_model_the_shape_does_not_declare_is_refused_by_name() -> None:
    shape = Shape(Table(Company, rows=5, name=Constant("acme")))

    with pytest.raises(InvalidShape) as raised:
        fan_out_sizes(shape, Session, "company")

    assert "Session" in str(raised.value)
    # And what the shape does hold, because the usual cause is asking about the
    # parent instead of the child.
    assert Company._meta.db_table in str(raised.value)


def test_a_projection_has_no_partition_to_invert() -> None:
    shape = Shape(Projection(EventSession, per=Event, copying=TemplateSession))

    with pytest.raises(InvalidShape) as raised:
        fan_out_sizes(shape, EventSession, "event")

    # A different sentence from the one above, because the answer is different:
    # the model is declared, and its foreign keys are copied along a join rather
    # than spread by anything this could invert.
    assert "Projection" in str(raised.value)


def test_a_column_that_is_not_a_fan_out_is_refused_and_lists_what_is_declared() -> None:
    shape = Shape(Table(Company, rows=5, name=Constant("acme")))

    with pytest.raises(InvalidShape) as raised:
        fan_out_sizes(shape, Company, "name")

    assert "FanOut" in str(raised.value)
    assert "name" in str(raised.value)


@pytest.mark.django_db(transaction=True)
@_POSTGRES_ONLY
def test_the_counts_are_the_rows_the_build_actually_wrote() -> None:
    # The claim the whole surface rests on. The counts are derived from the
    # declaration and the parent keys rather than read off the child table, so
    # the only thing worth asserting is that the derivation and the table agree
    # -- for every parent, including the ones with nothing.
    shape = _graph()
    build_shape(shape)

    counts = fan_out_sizes(shape, Session, "company")

    assert {parent: count for parent, count in counts.items() if count} == _loaded()
    assert sum(counts.values()) == 3000
    assert set(counts) == set(Company.objects.values_list("pk", flat=True))


@pytest.mark.django_db(transaction=True)
@_POSTGRES_ONLY
def test_the_head_of_the_distribution_is_reachable_without_aggregating_the_children() -> None:
    shape = _graph()
    build_shape(shape)

    counts = fan_out_sizes(shape, Session, "company")

    whale, children = counts.ranked()[0]
    # The sentence a plan assertion wants to write: this parent, that many
    # children, and a query over it takes a different plan from one over the
    # body of the distribution.
    assert children == Session.objects.filter(company_id=whale).count()
    assert children == max(counts.values())
    # A Zipf head is a head, not a slightly larger average. Without that the
    # skew is not doing the thing it was declared for.
    assert children > 3000 / 40 * 3


@pytest.mark.django_db(transaction=True)
@_POSTGRES_ONLY
def test_the_childless_tail_is_named_and_really_is_childless() -> None:
    shape = _graph()
    build_shape(shape)

    counts = fan_out_sizes(shape, Session, "company")

    childless = counts.childless()
    assert childless
    assert not Session.objects.filter(company_id__in=childless).exists()
    assert set(childless).isdisjoint(_loaded())


@pytest.mark.django_db(transaction=True)
@_POSTGRES_ONLY
def test_the_sizes_are_not_rank_ordered_on_the_parent_key() -> None:
    # Documented rather than fixed, and pinned here so it cannot be "fixed" by
    # accident. Ordering the sizes on the parent key would put a correlation
    # between a company's id and its size into the child table that no real
    # system has, which is the opposite of what this package is for -- so "the
    # whales are the low ids" is false, in both directions.
    shape = _graph()
    build_shape(shape)

    counts = fan_out_sizes(shape, Session, "company")

    in_key_order = list(counts.values())
    assert in_key_order != sorted(in_key_order, reverse=True)
    assert in_key_order != sorted(in_key_order)
    # Which is the same thing said from the other side: the ranking is a real
    # reordering of the parent keys rather than them read forwards or backwards.
    # Deliberately not an assertion about where any particular parent landed --
    # the head can fall on the lowest key by chance, and pinning that would be
    # asserting the seed rather than the mechanism.
    ranked_keys = [parent for parent, _count in counts.ranked()]
    assert ranked_keys != list(counts)
    assert ranked_keys != list(reversed(list(counts)))


@pytest.mark.django_db(transaction=True)
@_POSTGRES_ONLY
def test_parents_built_outside_the_shape_are_taken_as_they_are() -> None:
    # The hybrid this package is written for: the parents come from the ORM and
    # only the large table is declared. Nothing in the declaration says how many
    # companies there should be, so nothing is checked -- the answer is about the
    # parents that are there.
    made = [Company.objects.create(name=f"c{index}") for index in range(12)]
    shape = Shape(Table(Session, rows=600, company=FanOut(Zipf(1.2)), label=Constant("s")), seed=6)
    build_shape(shape)

    counts = fan_out_sizes(shape, Session, "company")

    assert set(counts) == {company.pk for company in made}
    assert {parent: count for parent, count in counts.items() if count} == _loaded()


@pytest.mark.django_db(transaction=True)
@_POSTGRES_ONLY
def test_a_parent_table_that_has_moved_since_the_build_is_refused_rather_than_answered() -> None:
    # The failure recomputation makes possible, so it is the one that has to be
    # loud. Every number would still add up; they would add up over a partition
    # of a world nobody built, and nothing about the result would look wrong.
    shape = _graph()
    build_shape(shape)
    Company.objects.create(name="created after the build")

    with pytest.raises(WorldChanged) as raised:
        fan_out_sizes(shape, Session, "company")

    assert "41" in str(raised.value)
    assert "40" in str(raised.value)


@pytest.mark.django_db(transaction=True)
@_POSTGRES_ONLY
def test_a_null_share_leaves_the_counts_an_upper_bound_and_says_so() -> None:
    # The one case where a count here is not a row count. A null share thins the
    # partition per row after it is computed, so the sizes are the partition and
    # the rows are fewer -- carried on the result rather than left in the
    # declaration, because the caller comparing the two has the result in hand.
    shape = Shape(
        Table(Company, rows=20, name=Constant("acme")),
        Table(
            OptionalChild,
            rows=1200,
            company=FanOut(Zipf(1.2), null=0.25),
            label=Constant("o"),
        ),
        seed=7,
    )
    build_shape(shape)

    counts = fan_out_sizes(shape, OptionalChild, "company")

    assert counts.null_share == 0.25
    assert sum(counts.values()) == 1200
    loaded = OptionalChild.objects.exclude(company=None).count()
    assert loaded < 1200
    for parent, count in counts.items():
        assert OptionalChild.objects.filter(company_id=parent).count() <= count


@pytest.fixture
def temporary_databases() -> Iterator[list[str]]:
    """Every database a test makes, dropped afterwards.

    A template is a content-keyed cache with nothing to garbage-collect against,
    so the package deliberately never removes one and a suite that makes them
    has to.
    """
    made: list[str] = []
    yield made
    for name in reversed(made):
        drop_database(name)


@contextmanager
def _pointed_at(database: str) -> Iterator[None]:
    """Run the rest of the block against a cloned database.

    Rewriting ``settings_dict["NAME"]`` and closing is Django's own way of
    moving a connection to another database -- it is what its test runner does
    and what filling a template does here. It stands in for what a consumer
    configures once in their settings, and it is what makes the block below a
    real cache-path test: this process reads a database it did not generate.
    """
    default = connections["default"]
    original = default.settings_dict["NAME"]
    default.close()
    default.settings_dict["NAME"] = database
    try:
        yield
    finally:
        default.close()
        default.settings_dict["NAME"] = original


@pytest.mark.django_db(transaction=True)
@_POSTGRES_ONLY
def test_the_inversion_survives_the_template_database_cache(
    temporary_databases: list[str],
) -> None:
    # The question this function was designed against. A cache hit clones a
    # database and generates nothing, so anything carried off a build result
    # would simply not exist on this path. Recomputation does not care: the
    # clone holds the parents, the declaration holds the rest.
    shape = _graph(companies=25, sessions=1500, seed=8)
    name = template_database(shape)
    temporary_databases.append(name)
    target = f"{name}_inversion"
    temporary_databases.append(target)
    clone_database(name, target)

    with _pointed_at(target):
        counts = fan_out_sizes(shape, Session, "company")
        loaded = _loaded()

    assert sum(counts.values()) == 1500
    assert {parent: count for parent, count in counts.items() if count} == loaded
    assert counts.childless()
