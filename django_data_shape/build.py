"""Turning a declaration into a database the planner can reason about."""

from __future__ import annotations

from typing import Any, cast

from django.core.management.color import no_style
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import Model

from django_data_shape.build_result import BuildResult
from django_data_shape.fan_out import FanOut
from django_data_shape.fan_out_plan import FanOutPlan
from django_data_shape.generate_rows import generate_rows
from django_data_shape.order_tables import order_tables
from django_data_shape.require_postgres import require_postgres
from django_data_shape.resolve_fan_out import resolve_fan_out
from django_data_shape.shape import Shape
from django_data_shape.shape_not_empty import ShapeNotEmpty
from django_data_shape.table import Table
from django_data_shape.table_result import TableResult


def build(shape: Shape, using: str = DEFAULT_DB_ALIAS) -> BuildResult:
    """Generate, load, reset sequences and analyze every table in ``shape``.

    The order of those steps is the whole function, and it is not
    interchangeable. Loading rows into a table that was analyzed while empty
    leaves the planner holding statistics from the old contents and applying
    them to the new row count -- a worse lie than having no statistics at all,
    and the one that produced a thirty-thousand-fold misestimate in the
    measurements this package was designed from. So the ``ANALYZE`` is here, at
    the end, owned by the library rather than left to the caller to remember.

    That is also why a bare ``ANALYZE`` is in this release at all, when the
    statistics work proper -- per-column targets, and caching a built database
    as a template -- comes later. A loader that leaves its table unanalyzed
    ships the exact state this package exists to condemn.
    """
    connection = connections[using]
    require_postgres(connection, "Building a shape")

    results: list[TableResult] = []
    # One transaction around every table. Without it a shape whose second table
    # fails leaves the first committed and analyzed, and the natural next action
    # -- fix the shape, run it again -- fails on a duplicate key rather than on
    # the original problem.
    with transaction.atomic(using=using):
        # Parents first. Not because the database insists -- Django's foreign
        # keys are deferred, so any order commits -- but because a fan-out reads
        # its parent's real keys, and a table with no rows yet has none.
        for table in order_tables(shape.tables):
            _require_empty(connection, table)
            plans = _resolve(connection, table, shape.seed)
            loaded = _load(connection, table, shape.seed, plans)
            _reset_sequence(connection, table)
            _analyze(connection, table)
            results.append(TableResult(table=table.db_table, rows=loaded))
    return BuildResult(tables=tuple(results))


def _resolve(connection: Any, table: Table, seed: int) -> dict[str, FanOutPlan]:
    """Partition each declared relation over the parent keys that exist."""
    plans: dict[str, FanOutPlan] = {}
    for name, field in table.relations():
        plans[name] = resolve_fan_out(
            cast("FanOut", table.fields[name]),
            cast("type[Model]", field.related_model),
            table.rows,
            seed,
            table.db_table,
            name,
            connection,
        )
    return plans


def _require_empty(connection: Any, table: Table) -> None:
    """Refuse to build on top of rows that are already there.

    The keys this package assigns start at 1 every time, so a second build over
    the same table collides on the primary key. That surfaced as a bare
    UniqueViolation naming an index, which tells the reader nothing about what
    they did or what to do instead.
    """
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT EXISTS (SELECT 1 FROM {connection.ops.quote_name(table.db_table)})")
        row = cursor.fetchone()
    if row[0]:
        raise ShapeNotEmpty(
            f"{table.db_table} already holds rows, and this package assigns primary keys from 1, "
            "so building over them would collide. Empty the table first."
        )


# The connection is typed loosely on purpose. ``cursor.copy()`` is psycopg 3's
# API reached through Django's cursor wrapper, and it appears on no Django base
# class, so annotating the real wrapper type would mean asserting the checker
# out of the way on every line that uses it.
def _load(connection: Any, table: Table, seed: int, plans: dict[str, FanOutPlan]) -> int:
    """Stream generated rows into the table with ``COPY FROM STDIN``.

    ``bulk_create`` is the obvious alternative and is roughly an order of
    magnitude too slow at the row counts that make a plan meaningful. ``COPY``
    is also why the generator yields tuples rather than model instances: there
    is no instance to build, and no ``save`` to run.

    Each declared value is passed through its field's ``get_db_prep_save``
    first, and that is not a formality. Skipping it is how a naive datetime got
    written five hours away from where ``save()`` would have put it under a
    non-UTC ``TIME_ZONE`` -- silently, on the exact column ``Sequential`` exists
    to make realistic -- and how a ``JSONField`` failed to load at all. The
    generator stays backend-neutral because the preparation happens here rather
    than inside it.

    Returns the number of rows the database actually took, not the number
    declared. They are the same today and stop being so once deduplicated
    many-to-many edges arrive.
    """
    quote = connection.ops.quote_name
    pk_column = table.model._meta.pk.column
    columns = [quote(pk_column)] + [quote(field.column) for _, field in table.columns()]
    statement = f"COPY {quote(table.db_table)} ({', '.join(columns)}) FROM STDIN"
    prepare = [field.get_db_prep_save for _, field in table.columns()]

    with connection.cursor() as cursor:
        # ``copy`` is not in Django's WRAP_ERROR_ATTRS, so without this a
        # Postgres error escapes as a raw psycopg exception: the caller cannot
        # catch django.db.IntegrityError, and -- worse -- an enclosing atomic
        # block never learns it needs a rollback, so the next query inside it
        # fails with "current transaction is aborted" instead of a Django error.
        with connection.wrap_database_errors, cursor.copy(statement) as copy:
            for row in generate_rows(table, seed, plans):
                copy.write_row(
                    (
                        row[0],
                        *(
                            prep(value, connection)
                            for prep, value in zip(prepare, row[1:], strict=True)
                        ),
                    )
                )
        return int(cursor.rowcount)


def _reset_sequence(connection: Any, table: Table) -> None:
    """Move the identity sequence past the keys this package just assigned.

    Skipping this is the first bug the design invites: rows exist at ids 1..N
    while the sequence still starts at 1, so the very first ``objects.create()``
    inside a test raises ``IntegrityError`` on a primary key that is already
    taken. Django's own backend operation is used rather than a hand-written
    ``setval`` because it already knows how the column's sequence is named.
    """
    with connection.cursor() as cursor:
        for statement in connection.ops.sequence_reset_sql(no_style(), [table.model]):
            cursor.execute(statement)


def _analyze(connection: Any, table: Table) -> None:
    """Populate the statistics the planner reads.

    Rows alone change nothing: without this the planner falls back to a default
    selectivity and commits to it, which is how a two-million-row table gets
    bitmap-scanned through an index for a value matching 98% of it. Measured at
    81 ms on that table, because ``ANALYZE`` samples rather than scans.
    """
    with connection.cursor() as cursor:
        cursor.execute(f"ANALYZE {connection.ops.quote_name(table.db_table)}")
