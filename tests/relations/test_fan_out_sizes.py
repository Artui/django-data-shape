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
    Skew,
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
    Supply,
    TemplateSession,
    Vendor,
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
    """What the child table actually holds, the expensive way, for comparison."""
    return dict(
        Session.objects.values("company_id")
        .annotate(loaded=Count("pk"))
        .values_list("company_id", "loaded")
    )


# Refusals read the declaration before any connection, so no database is needed.
def test_a_model_the_shape_does_not_declare_is_refused_by_name() -> None:
    shape = Shape(Table(Company, rows=5, name=Constant("acme")))

    with pytest.raises(InvalidShape) as raised:
        fan_out_sizes(shape, Session, "company")

    assert "Session" in str(raised.value)
    assert Company._meta.db_table in str(raised.value)


def test_a_projection_has_no_partition_to_invert() -> None:
    shape = Shape(Projection(EventSession, per=Event, copying=TemplateSession))

    with pytest.raises(InvalidShape) as raised:
        fan_out_sizes(shape, EventSession, "event")

    # A projection's foreign keys are copied along a join, so nothing here can invert them.
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
    # The claim the whole surface rests on.
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
    # A Zipf head is a head, not a slightly larger average.
    assert children == Session.objects.filter(company_id=whale).count()
    assert children == max(counts.values())
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
    # Documented rather than fixed, and pinned here so it cannot be "fixed" by accident.
    shape = _graph()
    build_shape(shape)

    counts = fan_out_sizes(shape, Session, "company")

    in_key_order = list(counts.values())
    assert in_key_order != sorted(in_key_order, reverse=True)
    assert in_key_order != sorted(in_key_order)
    ranked_keys = [parent for parent, _count in counts.ranked()]
    assert ranked_keys != list(counts)
    assert ranked_keys != list(reversed(list(counts)))


@pytest.mark.django_db(transaction=True)
@_POSTGRES_ONLY
def test_parents_built_outside_the_shape_are_taken_as_they_are() -> None:
    # Parents come from the ORM and only the large table is declared, so nothing is checked.
    made = [Company.objects.create(name=f"c{index}") for index in range(12)]
    shape = Shape(Table(Session, rows=600, company=FanOut(Zipf(1.2)), label=Constant("s")), seed=6)
    build_shape(shape)

    counts = fan_out_sizes(shape, Session, "company")

    assert set(counts) == {company.pk for company in made}
    assert {parent: count for parent, count in counts.items() if count} == _loaded()


@pytest.mark.django_db(transaction=True)
@_POSTGRES_ONLY
def test_a_parent_table_that_has_moved_since_the_build_is_refused_rather_than_answered() -> None:
    # The failure recomputation makes possible, so it is the one that has to be loud.
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
    # The one case where a count here is not a row count.
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
    """Run the rest of the block against a cloned database."""
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
    # The question this function was designed against.
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


@_POSTGRES_ONLY
@pytest.mark.django_db(transaction=True)
def test_a_narrowed_fan_out_is_not_a_world_that_changed() -> None:
    """The check has to compare against what was narrowed to, not the table."""
    shape = Shape(
        Table(Company, rows=50, name=Constant("c")),
        Table(
            Session,
            rows=100,
            company=FanOut(Constant(1), parents=list(range(1, 11))),
            label=Constant("s"),
        ),
        seed=3,
    )
    build_shape(shape)

    counts = fan_out_sizes(shape, Session, "company")

    assert len(counts) == 10
    assert sum(counts.values()) == 100


@pytest.mark.django_db
def test_a_capped_fan_out_reports_the_capped_sizes() -> None:
    # The cap must show in fan_out_sizes' answer, which reads the partition the build wrote.
    shape = Shape(
        Table(Company, rows=40, name=Constant("acme")),
        Table(
            Session,
            rows=600,
            company=FanOut(Zipf(1.2), max_children=25),
            label=Constant("s"),
        ),
        seed=5,
    )
    build_shape(shape, require_statistics=False)

    counts = fan_out_sizes(shape, Session, "company")

    assert counts.ranked()[0][1] == 25
    assert {key: size for key, size in counts.ranked() if size} == _loaded()


def _capped(companies: int = 10, sessions: int = 50) -> Shape:
    return Shape(
        Table(Company, rows=companies, name=Constant("acme")),
        Table(
            Session,
            rows=sessions,
            company=FanOut(Zipf(1.2), max_children=5),
            label=Constant("s"),
        ),
        seed=5,
    )


@pytest.mark.django_db(transaction=True)
@_POSTGRES_ONLY
def test_a_cap_the_shrunk_parents_cannot_hold_is_the_world_changing() -> None:
    # Ten parents capped at five hold the fifty rows exactly; nine cannot.
    shape = _capped()
    build_shape(shape, require_statistics=False)
    Company.objects.filter(pk=Company.objects.order_by("pk").values("pk")[:1]).delete()

    with pytest.raises(WorldChanged) as raised:
        fan_out_sizes(shape, Session, "company")

    assert "holds 9 rows" in str(raised.value)
    assert isinstance(raised.value.__cause__, InvalidShape)


@pytest.mark.django_db(transaction=True)
@_POSTGRES_ONLY
def test_an_emptied_parent_table_is_the_world_changing_too() -> None:
    # The partition refuses an empty parent before any count comparison runs.
    shape = _capped()
    build_shape(shape, require_statistics=False)
    Company.objects.all().delete()

    with pytest.raises(WorldChanged, match="holds 0 rows"):
        fan_out_sizes(shape, Session, "company")


@pytest.mark.django_db(transaction=True)
@_POSTGRES_ONLY
def test_a_cap_refused_over_the_parents_the_shape_declares_is_still_the_shape() -> None:
    # The parent table holds what the shape says, so the refusal is the declaration's own.
    build_shape(Shape(Table(Company, rows=10, name=Constant("acme"))), require_statistics=False)

    with pytest.raises(InvalidShape, match="at most 50 rows fit"):
        fan_out_sizes(_capped(sessions=51), Session, "company")


@pytest.mark.django_db(transaction=True)
@_POSTGRES_ONLY
def test_a_cap_refused_over_parents_built_elsewhere_is_the_shape() -> None:
    # With parents outside the shape there is no declared count to have moved from.
    Company.objects.bulk_create([Company(name="acme") for _ in range(10)])
    shape = Shape(
        Table(
            Session,
            rows=51,
            company=FanOut(Zipf(1.2), max_children=5),
            label=Constant("s"),
        ),
    )

    with pytest.raises(InvalidShape, match="at most 50 rows fit"):
        fan_out_sizes(shape, Session, "company")


@pytest.mark.django_db(transaction=True)
@_POSTGRES_ONLY
def test_the_moved_world_counts_the_parents_a_default_manager_hides() -> None:
    # The partition reads every parent row, so the count must bypass the default manager.
    shape = Shape(
        Table(Vendor, rows=10, name=Constant("v"), retired=Skew({True: 1, False: 1})),
        Table(
            Supply,
            rows=50,
            vendor=FanOut(Zipf(1.2), max_children=5),
            vendor_name=Constant("v"),
        ),
        seed=5,
    )
    build_shape(shape, require_statistics=False)
    assert Vendor.objects.count() < 9
    Vendor._base_manager.filter(pk=Vendor._base_manager.order_by("pk").values("pk")[:1]).delete()

    with pytest.raises(WorldChanged, match="holds 9 rows"):
        fan_out_sizes(shape, Supply, "vendor")
