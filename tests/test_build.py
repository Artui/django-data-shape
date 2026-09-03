"""The end-to-end claim: a declaration becomes a database the planner can read."""

from __future__ import annotations

import datetime

import pytest
from django.db import DatabaseError, connection

from django_data_shape import (
    Constant,
    KeyFunction,
    Sequential,
    Shape,
    ShapeNotEmpty,
    Skew,
    Table,
    Uniform,
    UnsupportedBackend,
)
from django_data_shape import build as build_shape
from tests.testapp.models import Company, Order, Prepared, Tenant

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


_AWARE = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
_ORDERS = Order._meta.db_table


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


def test_a_naive_datetime_is_stored_where_save_would_have_put_it() -> None:
    # The silent one. Written without Django's field preparation a naive
    # datetime lands verbatim, which under this suite's America/Chicago
    # TIME_ZONE is hours away from where save() puts it -- on the exact column
    # Sequential exists to make realistic, with no error and no warning.
    naive = datetime.datetime(2020, 1, 1, 12, 0)
    # Django's own warning, raised from DateTimeField.get_prep_value. Before the
    # fix build() raised none at all, which is the sharpest evidence that no
    # field preparation was happening.
    with pytest.warns(RuntimeWarning, match="received a naive datetime"):
        build_shape(Shape(Table(Prepared, rows=1, at=Constant(naive), tags=Constant({"a": 1}))))

    saved = Prepared.objects.create(at=naive, tags={"b": 2})
    # Both sides read back from the database: the point is what was *stored*,
    # and the in-memory instance still holds the naive value it was handed.
    saved.refresh_from_db()
    loaded = Prepared.objects.exclude(pk=saved.pk).get()

    assert loaded.at == saved.at
    assert loaded.at == datetime.datetime(2020, 1, 1, 18, 0, tzinfo=datetime.timezone.utc)


def test_a_json_column_loads_at_all() -> None:
    # psycopg has no adapter for a bare dict, so without preparation this does
    # not merely land wrong -- the build fails outright.
    build_shape(Shape(Table(Prepared, rows=3, at=Constant(_AWARE), tags=Constant({"a": [1, 2]}))))

    assert Prepared.objects.get(pk=1).tags == {"a": [1, 2]}


def test_analyze_is_fresh_rather_than_merely_present() -> None:
    # pg_statistic survives both TRUNCATE and --reuse-db, so asserting that
    # statistics *exist* passes against a build that never analyzed, as long as
    # some earlier run did. Asking when the table was last analyzed is the
    # question that actually discriminates.
    build_shape(Shape(_orders(rows=200)))
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_stat_get_last_analyze_time(%s::regclass)", [_ORDERS])
        first = cursor.fetchone()[0]

    Order.objects.all().delete()
    build_shape(Shape(_orders(rows=200)))
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_stat_get_last_analyze_time(%s::regclass)", [_ORDERS])
        second = cursor.fetchone()[0]

    assert first is not None
    assert second > first


def test_building_over_existing_rows_is_refused_before_anything_is_written() -> None:
    # Keys start at 1 every time, so the second build collided on the primary
    # key and reported a unique violation naming an index -- which says nothing
    # about what the caller did or what to do instead.
    build_shape(Shape(_orders(rows=10)))

    with pytest.raises(ShapeNotEmpty, match="already holds rows"):
        build_shape(Shape(_orders(rows=10)))


def test_a_failed_build_leaves_no_table_behind() -> None:
    # Without the transaction the first table stayed committed and analyzed, so
    # the natural next action -- fix the shape and run it again -- failed on a
    # duplicate key rather than on the original problem.
    Order.objects.create(
        status="complete", total="1.00", created_at=_AWARE
    )  # makes the second table refuse

    with pytest.raises(ShapeNotEmpty):
        build_shape(Shape(Table(Company, rows=5, name=Constant("acme")), _orders(rows=10)))

    assert Company.objects.count() == 0


def test_a_copy_failure_arrives_as_a_django_error() -> None:
    # cursor.copy is not in Django's WRAP_ERROR_ATTRS, so the psycopg exception
    # escaped raw: uncatchable as django.db.DatabaseError, and an enclosing
    # atomic block never learned it needed a rollback.
    too_long = Table(Company, rows=1, name=Constant("x" * 500))

    with pytest.raises(DatabaseError):
        build_shape(Shape(too_long))


def test_the_result_counts_what_the_database_took() -> None:
    result = build_shape(Shape(_orders(rows=250)))

    assert result.rows == Order.objects.count() == 250


def test_a_uuid_keyed_table_may_be_built_beside_rows_the_caller_made() -> None:
    """The hybrid the documentation advertises, for the schemas that use UUIDs.

    ``_require_empty`` refuses a build over existing rows because this package
    assigns keys from 1 and a second build collides -- and that reasoning is
    about integer keys, where the refusal was not. A UuidKeys table derives a
    128-bit digest per row and cannot land on a factory's row, so refusing it
    blocked "parents your code made, children this package made" in exactly the
    schemas where UUID primary keys are the norm.
    """
    theirs = Tenant.objects.create(name="made by the caller's own factory")

    build_shape(Shape(Table(Tenant, rows=10, name=Constant("t")), seed=1))

    assert Tenant.objects.count() == 11
    assert Tenant.objects.filter(pk=theirs.pk).exists()


def test_an_integer_keyed_table_is_still_refused_and_says_why() -> None:
    """The half the gate must not widen: keys from 1 do collide."""
    Company.objects.create(name="already here")

    with pytest.raises(ShapeNotEmpty, match="assigns primary keys from 1"):
        build_shape(Shape(Table(Company, rows=10, name=Constant("c")), seed=1))


def test_a_key_function_is_read_as_able_to_collide() -> None:
    """The case that decides the protocol is opt-in rather than a default.

    A caller's own function could return anything, this package cannot read it,
    and guessing "probably fine" would trade a clear refusal for a load that
    dies partway through.
    """
    Company.objects.create(name="already here")

    with pytest.raises(ShapeNotEmpty):
        build_shape(
            Shape(
                Table(Company, rows=3, name=Constant("c"), keys=KeyFunction(lambda row: row + 500)),
                seed=1,
            )
        )
