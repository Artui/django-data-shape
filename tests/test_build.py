"""The end-to-end claim: a declaration becomes a database the planner can read."""

from __future__ import annotations

import datetime

import pytest
from django.db import connection

from django_data_shape import Constant, Sequential, Shape, Skew, Table, Uniform, UnsupportedBackend
from django_data_shape import build as build_shape
from tests.testapp.models import Company, Order

# Skipped with a reason on any other backend rather than silently passing. This
# module is the package's own claim under test -- COPY, a reset sequence, real
# planner statistics -- and none of it means anything where those do not exist.
# A performance assertion that passes because the backend could not check it is
# exactly the failure this package was written to expose, so its suite does not
# get to make it. The Postgres CI job is what carries the coverage gate.
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="COPY loading and planner statistics need PostgreSQL",
    ),
]


def _orders(rows: int = 5000) -> Table:
    return Table(
        Order,
        rows=rows,
        status=Skew({"complete": 0.98, "pending": 0.015, "cancelled": 0.005}),
        total=Uniform(0, 500, places=2),
        created_at=Sequential(
            datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
            datetime.timedelta(seconds=3),
        ),
    )


def test_it_loads_the_declared_number_of_rows() -> None:
    result = build_shape(Shape(_orders(rows=5000)))

    assert Order.objects.count() == 5000
    assert result.rows == 5000
    assert result.tables[0].table == Order._meta.db_table


def test_the_declared_skew_is_what_lands_in_the_table() -> None:
    build_shape(Shape(_orders(rows=10_000)))

    counts = {
        status: Order.objects.filter(status=status).count()
        for status in ("complete", "pending", "cancelled")
    }

    # The whole point of the package in one assertion: the rare value is rare,
    # which is what makes an index on this column usable and what a fixtures
    # loop with one row of each says the opposite of.
    assert counts["complete"] > 9600
    assert 100 < counts["pending"] < 200
    assert 0 < counts["cancelled"] < 100


def test_analyze_runs_so_the_planner_can_see_the_skew() -> None:
    build_shape(Shape(_orders(rows=10_000)))

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT most_common_vals, most_common_freqs FROM pg_stats "
            "WHERE tablename = %s AND attname = 'status'",
            [Order._meta.db_table],
        )
        row = cursor.fetchone()

    # Falsifiable: with the ANALYZE removed from build(), pg_stats holds no row
    # for this column at all and this is None. Rows without statistics is the
    # state the package exists to condemn, so its own loader must not leave one.
    assert row is not None
    values, frequencies = row
    assert "complete" in values
    assert max(frequencies) > 0.9


def test_the_sequence_is_moved_past_the_keys_it_assigned() -> None:
    build_shape(Shape(_orders(rows=100)))

    # The first bug the dense-primary-key design invites. Without the reset this
    # raises IntegrityError on a key that already exists, and it would do so in
    # the consumer's test rather than here.
    created = Order.objects.create(
        status="complete",
        total="1.00",
        created_at=datetime.datetime(2021, 1, 1, tzinfo=datetime.timezone.utc),
    )

    assert created.pk > 100


def test_two_builds_of_one_shape_agree_row_for_row() -> None:
    shape = Shape(_orders(rows=500), seed=42)

    build_shape(shape)
    first = list(Order.objects.order_by("pk").values_list("id", "status", "total", "created_at"))
    Order.objects.all().delete()
    build_shape(shape)
    second = list(Order.objects.order_by("pk").values_list("id", "status", "total", "created_at"))

    assert first == second


def test_every_declared_table_is_built() -> None:
    result = build_shape(Shape(_orders(rows=10), Table(Company, rows=7, name=Constant("acme"))))

    assert Order.objects.count() == 10
    assert Company.objects.count() == 7
    assert result.rows == 17
    assert {table.rows for table in result.tables} == {10, 7}


def test_building_against_a_non_postgres_connection_is_refused() -> None:
    # The gate is unit-tested with a stub elsewhere. This test exists because a
    # stub proves the gate works, not that anything calls it: it drives the real
    # entry point into a real refusal, so deleting the call from build() fails
    # here even though the stub test would still pass.
    with pytest.raises(UnsupportedBackend, match="needs PostgreSQL"):
        build_shape(Shape(_orders(rows=1)), using="not_postgres")
