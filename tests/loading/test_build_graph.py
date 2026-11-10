from __future__ import annotations

from collections import Counter

import pytest
from django.db import connection

from django_data_shape import (
    Constant,
    FanOut,
    InvalidShape,
    Shape,
    Table,
    Uniform,
    Zipf,
)
from django_data_shape import build as build_shape
from tests.testapp.models import Company, OptionalChild, Session, Tenant, TenantRecord

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="COPY loading and planner statistics need PostgreSQL",
    ),
]


def _sessions(rows: int, fan_out: FanOut | None = None) -> Table:
    return Table(
        Session,
        rows=rows,
        company=fan_out or FanOut(Zipf(1.2)),
        label=Constant("s"),
    )


def test_a_parent_and_child_load_together_in_dependency_order() -> None:
    result = build_shape(Shape(_sessions(2000), Table(Company, rows=50, name=Constant("acme"))))

    # Declared child-first, yet loaded parent first, since a fan-out reads its parent's keys.
    assert Company.objects.count() == 50
    assert Session.objects.count() == 2000
    assert result.rows == 2050


def test_every_child_points_at_a_real_parent() -> None:
    build_shape(Shape(Table(Company, rows=30, name=Constant("acme")), _sessions(1500)))

    parents = set(Company.objects.values_list("id", flat=True))
    children = set(Session.objects.values_list("company_id", flat=True))

    # Referential integrity by construction: every key came out of the parent table.
    assert children <= parents


def test_the_parent_keys_are_read_rather_than_assumed() -> None:
    # The correction this milestone exists for.
    with connection.cursor() as cursor:
        cursor.execute(f"ALTER SEQUENCE {Company._meta.db_table}_id_seq RESTART WITH 90000")
    made = [Company.objects.create(name=f"c{i}") for i in range(20)]
    assert min(c.pk for c in made) >= 90000

    build_shape(Shape(_sessions(600)))

    children = set(Session.objects.values_list("company_id", flat=True))
    assert children <= {c.pk for c in made}
    assert min(children) >= 90000


def test_the_fan_out_has_a_head_and_a_tail_in_the_database() -> None:
    build_shape(Shape(Table(Company, rows=200, name=Constant("acme")), _sessions(20_000)))

    counts = sorted(
        Company.objects.values_list("id", flat=True).annotate(),
        key=lambda _: 0,
    )
    del counts
    per_parent = sorted(
        (
            Session.objects.filter(company_id=company_id).count()
            for company_id in Company.objects.values_list("id", flat=True)
        ),
        reverse=True,
    )

    # Uniform fan-out makes the planner always right, because its n_distinct average is the truth.
    assert per_parent[0] > 10 * per_parent[len(per_parent) // 2]
    assert sum(per_parent) == 20_000


def test_childless_parents_survive_the_load() -> None:
    build_shape(
        Shape(
            Table(Company, rows=100, name=Constant("acme")),
            _sessions(3000, FanOut(Zipf(), childless=0.4)),
        )
    )

    without = Company.objects.filter(sessions__isnull=True).count()

    # The difference between an inner and an outer join, which the tail fixtures omit.
    assert 25 <= without <= 55


def test_a_nullable_relation_can_be_left_null_for_a_declared_share() -> None:
    build_shape(
        Shape(
            Table(Company, rows=20, name=Constant("acme")),
            Table(
                OptionalChild,
                rows=2000,
                company=FanOut(Uniform(1, 5), null=0.3),
                label=Constant("x"),
            ),
        )
    )

    nulls = OptionalChild.objects.filter(company__isnull=True).count()
    assert 500 < nulls < 700


def test_placement_changes_what_the_planner_sees() -> None:
    build_shape(
        Shape(
            Table(Company, rows=40, name=Constant("acme")),
            _sessions(20_000, FanOut(Uniform(1, 3), placement="grouped")),
        )
    )
    grouped = _correlation("company_id")

    Session.objects.all().delete()
    Company.objects.all().delete()
    build_shape(
        Shape(
            Table(Company, rows=40, name=Constant("acme")),
            _sessions(20_000, FanOut(Uniform(1, 3), placement="arrival")),
        )
    )
    arrival = _correlation("company_id")

    # Grouped is what the obvious nested loop produces and what no production table looks like.
    assert grouped > 0.9
    assert arrival < 0.3


def _correlation(column: str) -> float:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT correlation FROM pg_stats WHERE tablename = %s AND attname = %s",
            [Session._meta.db_table, column],
        )
        return float(cursor.fetchone()[0])


def test_a_self_referential_fan_out_is_refused() -> None:
    # It would read keys from a table still empty, so it is refused by name.
    from tests.testapp.models import Referred

    with pytest.raises(InvalidShape, match="points at its own table") as refused:
        Table(Referred, rows=5, label=Constant("x"), referrer=FanOut(Uniform(1, 2)))
    assert "Tree(" in str(refused.value)


def test_every_row_can_be_pinned_to_one_pre_existing_parent() -> None:
    """The declaration a tenant-scoped schema could not write."""
    tenants = [Company.objects.create(name=f"tenant-{index}") for index in range(4)]
    ours = tenants[2]

    build_shape(
        Shape(
            Table(
                Session,
                rows=50,
                company=FanOut(Constant(1), parents=[ours.pk]),
                label=Constant("s"),
            ),
            seed=1,
        )
    )

    assert Session.objects.count() == 50
    assert set(Session.objects.values_list("company_id", flat=True)) == {ours.pk}


def test_a_narrowed_shape_builds_the_same_over_parents_whose_keys_differ() -> None:
    """The promise this package rests on, checked where it was being broken."""
    shapes = []
    for _ in range(6):
        TenantRecord.objects.all().delete()
        Tenant.objects.all().delete()
        tenants = [Tenant.objects.create(name=f"t{index}") for index in range(4)]
        named = [tenant.pk for tenant in tenants]
        build_shape(
            Shape(
                Table(
                    TenantRecord,
                    rows=100,
                    tenant=FanOut(Zipf(), parents=named),
                    label=Constant("s"),
                ),
                seed=5,
            )
        )
        counts = Counter(TenantRecord.objects.values_list("tenant_id", flat=True))
        shapes.append([counts[key] for key in named])

    assert len(set(map(tuple, shapes))) == 1, shapes


def test_pinning_leaves_the_other_parents_alone_rather_than_childless() -> None:
    """childless= is a share of the parents named, not of the table."""
    tenants = [Company.objects.create(name=f"tenant-{index}") for index in range(4)]
    named = [tenants[0].pk, tenants[1].pk]

    build_shape(
        Shape(
            Table(
                Session,
                rows=40,
                company=FanOut(Constant(1), parents=named, childless=0.5),
                label=Constant("s"),
            ),
            seed=4,
        )
    )

    used = set(Session.objects.values_list("company_id", flat=True))
    assert used <= set(named)
    assert len(used) == 1
