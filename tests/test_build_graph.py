"""Loading a model graph: the relation half of build()."""

from __future__ import annotations

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
from tests.testapp.models import Company, OptionalChild, Session

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

    # Declared child-first on purpose: the order the caller writes must not
    # decide the order the tables load, because a fan-out reads its parent's
    # keys and an unloaded parent has none.
    assert Company.objects.count() == 50
    assert Session.objects.count() == 2000
    assert result.rows == 2050


def test_every_child_points_at_a_real_parent() -> None:
    build_shape(Shape(Table(Company, rows=30, name=Constant("acme")), _sessions(1500)))

    parents = set(Company.objects.values_list("id", flat=True))
    children = set(Session.objects.values_list("company_id", flat=True))

    # Referential integrity by construction rather than by validation: every key
    # emitted came out of the parent table.
    assert children <= parents


def test_the_parent_keys_are_read_rather_than_assumed() -> None:
    # The correction this milestone exists for. The parents are created through
    # the ORM with keys nowhere near 1..N -- the realistic hybrid, where small
    # tables come from a service or model_bakery and the large ones from here.
    # A fan-out assuming a dense range would point every child at nothing.
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

    # Uniform fan-out makes the planner always right, because its n_distinct
    # average is the truth. A head and a tail is what makes a join estimate
    # capable of being wrong, which is the defect this package reproduces.
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

    # The tail fixtures always omit, and the difference between an inner and an
    # outer join returning the same thing.
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

    # Same rows, same per-parent counts, different physical order -- and
    # Postgres records it, costing an index scan over the column differently.
    # Grouped is what the obvious nested loop produces and what no production
    # table looks like.
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
    # Not a cycle between tables, but it cannot work either: it would read keys
    # from a table that is still empty. Refused by name rather than surfacing as
    # "the parent has no rows", which describes the symptom.
    from tests.testapp.models import Referred

    with pytest.raises(InvalidShape, match="points at its own table"):
        Table(Referred, rows=5, label=Constant("x"), referrer=FanOut(Uniform(1, 2)))


def test_every_row_can_be_pinned_to_one_pre_existing_parent() -> None:
    """The declaration a tenant-scoped schema could not write.

    Every heavy table in such a schema hangs off a tenant foreign key that has
    to point at one company the caller's own fixture made, and a fan-out spread
    over the whole parent table instead -- fifty rows landing 15/15/12/8 across
    four tenants, with only the last eleven visible to the caller.

    The parent here is made by the ORM rather than declared, which is the case
    that matters: its key is whatever the sequence handed out, so the narrowing
    has to be by real key rather than by position.
    """
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


def test_pinning_leaves_the_other_parents_alone_rather_than_childless() -> None:
    """childless= is a share of the parents named, not of the table.

    Worth pinning because the two readings differ in what they claim about the
    rows nobody asked about: a parent outside parents= has no children because
    it was not in the declaration, which is a different statement from a parent
    weighed at zero, and only the second is what childless= describes.
    """
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
    # Half of the two named parents is one of them, and the unnamed pair is
    # simply not part of this declaration at all.
    assert len(used) == 1
