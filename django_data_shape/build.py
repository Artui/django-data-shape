"""Turning a declaration into a database the planner can reason about."""

from __future__ import annotations

from typing import Any

from django.core.management.color import no_style
from django.db import DEFAULT_DB_ALIAS, connections

from django_data_shape.build_result import BuildResult
from django_data_shape.generate_rows import generate_rows
from django_data_shape.require_postgres import require_postgres
from django_data_shape.shape import Shape
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
    for table in shape.tables:
        _load(connection, table, shape.seed)
        _reset_sequence(connection, table)
        _analyze(connection, table)
        results.append(TableResult(table=table.db_table, rows=table.rows))
    return BuildResult(tables=tuple(results))


# The connection is typed loosely on purpose. ``cursor.copy()`` is psycopg 3's
# API reached through Django's cursor wrapper, and it appears on no Django base
# class, so annotating the real wrapper type would mean asserting the checker
# out of the way on every line that uses it.
def _load(connection: Any, table: Table, seed: int) -> None:
    """Stream generated rows straight into the table with ``COPY FROM STDIN``.

    ``bulk_create`` is the obvious alternative and is roughly an order of
    magnitude too slow at the row counts that make a plan meaningful. ``COPY``
    is also why the generator yields tuples rather than model instances: there
    is no instance to build, and no ``save`` to run.
    """
    quote = connection.ops.quote_name
    pk_column = table.model._meta.pk.column
    columns = [quote(pk_column)] + [quote(field.column) for _, field in table.columns()]
    statement = f"COPY {quote(table.db_table)} ({', '.join(columns)}) FROM STDIN"

    with connection.cursor() as cursor, cursor.copy(statement) as copy:
        for row in generate_rows(table, seed):
            copy.write_row(row)


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
