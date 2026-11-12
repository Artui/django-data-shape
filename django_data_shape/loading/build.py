from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from functools import partial
from itertools import islice
from typing import Any, cast

from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import Model

from django_data_shape.backends.require_postgres import require_postgres
from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.declaration.order_tables import order_tables
from django_data_shape.declaration.projection import Projection
from django_data_shape.declaration.shape import Shape
from django_data_shape.declaration.table import Table
from django_data_shape.generation.generate_rows import generate_rows
from django_data_shape.generation.refuse_queries import refuse_queries
from django_data_shape.invariants.check_invariants import check_invariants
from django_data_shape.keys.is_disjoint import is_disjoint
from django_data_shape.loading.apply_statistics_targets import apply_statistics_targets
from django_data_shape.loading.shape_not_empty import ShapeNotEmpty
from django_data_shape.relations.fan_out import FanOut
from django_data_shape.relations.fan_out_plan import FanOutPlan
from django_data_shape.relations.paired import Paired
from django_data_shape.relations.paired_plan import PairedPlan
from django_data_shape.relations.resolve_fan_out import resolve_fan_out
from django_data_shape.relations.resolve_paired import resolve_paired
from django_data_shape.types.build_result import BuildResult
from django_data_shape.types.table_result import TableResult
from django_data_shape.utils import reset_sequence

# Big enough that the per-statement overhead disappears and small enough that
# the peak list is a rounding error next to the rows themselves.
_INSERT_CHUNK = 1000


def build(
    shape: Shape, using: str = DEFAULT_DB_ALIAS, *, require_statistics: bool = True
) -> BuildResult:
    """Generate, load, reset sequences and analyze every table in ``shape``.

    The order of those steps is the whole function, and it is not
    interchangeable. Loading rows into a table that was analyzed while empty
    leaves the planner holding statistics from the old contents and applying
    them to the new row count -- a worse lie than having no statistics at all,
    and the one that produced a thirty-thousand-fold misestimate in the
    measurements this package was designed from. So the ``ANALYZE`` is here, at
    the end, owned by the library rather than left to the caller to remember.

    A bare ``ANALYZE`` shipped in 0.1.0 because a loader that leaves its table
    unanalyzed ships the exact state this package exists to condemn. What it
    gathers is bounded by each column's statistics target, and that is the other
    half:
    :func:`~django_data_shape.loading.apply_statistics_targets.apply_statistics_targets`
    puts the declared targets in place first, and refuses a declaration the
    planner could not record whatever it did afterwards.

    ``require_statistics=False`` asks for rows and cardinality rather than for a
    database the planner can reason about, and it is the only way to build on a
    backend without ``COPY`` and column statistics. It is written as a
    requirement being dropped rather than as work being skipped, because that is
    what it does: **on PostgreSQL it changes nothing at all** -- the load is
    still ``COPY`` and ``ANALYZE`` still runs, since both are free and leaving
    them out would manufacture the unanalyzed table this package exists to
    condemn. Elsewhere the rows are inserted instead and no statistics are
    gathered, so cardinality is real and nothing about a plan is claimed.

    The distinction is the one this package draws everywhere: generation and
    cardinality are backend-neutral, planner realism is not. A query *count* is
    an ORM property and means the same on any backend, which is why a growth
    assertion can be honest here while a plan assertion still cannot.

    A :class:`~django_data_shape.declaration.projection.Projection` sits in the same loop
    rather than in a pass of its own, and where it sits is decided by
    :func:`~django_data_shape.declaration.order_tables.order_tables` like everything else:
    after the tables it reads, and before anything that reads it. Only the step
    that produces the rows differs -- one statement instead of a generated
    stream -- and the three steps after it are the same steps for the same
    reasons. The emptiness check, because the keys still start at 1. The
    sequence reset, because the rows still sit at 1..N with the sequence at 1,
    and the first ``objects.create()`` in a test would still collide. And the
    ``ANALYZE`` above all, because a table filled by ``INSERT ... SELECT`` and
    left unanalyzed is exactly the unanalyzed million rows this package exists
    to condemn -- the route the rows took in has nothing to do with whether the
    planner can see them.
    """
    connection = connections[using]
    require_postgres(connection, "Building a shape", statistics=require_statistics)

    results: list[TableResult] = []
    with transaction.atomic(using=using):
        for table in order_tables(shape.tables):
            _require_empty(connection, table)
            apply_statistics_targets(connection, table)
            if isinstance(table, Projection):
                loaded = _project(connection, table, shape.seed)
            else:
                plans, pairings = _resolve(connection, table, shape.seed)
                loaded = _load(connection, table, shape.seed, plans, pairings)
            reset_sequence(connection, table.model)
            _analyze(connection, table.db_table)
            results.append(TableResult(table=table.db_table, rows=loaded))
        check_invariants(connection, shape.invariants)
    return BuildResult(tables=tuple(results))


def _require_within_ceiling(connection: Any, projection: Projection) -> None:
    """Refuse a projection that has outgrown its declared ceiling, before it runs."""
    ceiling = projection.max_rows
    if ceiling is None:
        return
    statement, params = projection.count_statement(connection)
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        row = cursor.fetchone()
    would_insert = int(row[0])
    if would_insert <= ceiling:
        return
    over = ", ".join(model.__name__ for model in projection.reads)
    raise InvalidShape(
        f"The projection into {projection.db_table} would insert {would_insert:,} rows, "
        f"which is over its declared max_rows={ceiling:,}. Nothing was written. "
        f"A projection has no row count of its own: its size is one row per pair along "
        f"the join it copies, over {over or 'the tables its statement selects from'}. "
        "That size is a product, so when both sides of the join fan out over the same "
        "parents the busy parents multiply and raising either declared count by four "
        "grows this by sixteen. Either the ceiling is too low, or one of those counts "
        "moved further than it looked."
    )


def _project(connection: Any, projection: Projection, seed: int) -> int:
    """Fill a table from tables already built, with one ``INSERT ... SELECT``."""
    _require_within_ceiling(connection, projection)
    statement, params = projection.statement(connection, seed)
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        inserted = int(cursor.rowcount)
    if inserted == 0:
        reads = ", ".join(model.__name__ for model in projection.reads)
        raise InvalidShape(
            f"The projection into {projection.db_table} inserted no rows, so this shape declared "
            "a table and then left it empty. It copies a collection along a join, and a join "
            f"matches nothing when either side is empty or when no rows pair up. It reads: "
            f"{reads or 'whatever the statement you supplied selects from, which it did not say'}"
            ". Declare those tables in the same shape so they are built first, or load them "
            "before building -- and if this is a statement of your own, name its inputs with "
            "reads= so it can be ordered after them rather than guessed at."
        )
    return inserted


def _resolve(
    connection: Any, table: Table, seed: int
) -> tuple[dict[str, FanOutPlan], dict[str, PairedPlan]]:
    """Partition each declared relation over the parent keys that exist."""
    parent_fields = table.parent_fields()
    plans: dict[str, FanOutPlan] = {}
    for name, field in table.relations():
        if not isinstance(table.fields[name], FanOut):
            continue
        plans[name] = resolve_fan_out(
            cast("FanOut", table.fields[name]),
            cast("type[Model]", field.related_model),
            table.rows,
            seed,
            table.db_table,
            name,
            connection,
            parent_fields.get(name, ()),
        )
    pairings: dict[str, PairedPlan] = {}
    for name, field in table.relations():
        declared = table.fields[name]
        if not isinstance(declared, Paired):
            continue
        pairings[name] = resolve_paired(
            declared,
            cast("type[Model]", field.related_model),
            plans[declared.relation],
            table.rows,
            seed,
            table.db_table,
            name,
            connection,
        )
    return plans, pairings


def _require_empty(connection: Any, table: Table | Projection) -> None:
    """Refuse to build on top of rows that are already there."""
    if is_disjoint(getattr(table, "keys", None)):
        return
    db_table = table.db_table
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT EXISTS (SELECT 1 FROM {connection.ops.quote_name(db_table)})")
        row = cursor.fetchone()
    if row[0]:
        raise ShapeNotEmpty(
            f"{db_table} already holds rows, and this package assigns primary keys from 1, "
            "so building over them would collide. If nothing in the test wrote them, the usual "
            "cause is a world that was already there: a session-scoped shape_fixture over this "
            "model holds its rows for the whole run, so a second build over it -- another "
            "shape_fixture, or build() called directly -- meets them; and a template started "
            "from a base database keeps the rows the base holds, apart from the tables it "
            "rebuilds for apps without migrations. Build the second world inside "
            "scaled_world, which empties the declared tables and puts them back; give the two "
            "different models; or empty this table first, in the base if that is where the "
            "rows came from."
        )


def _load(
    connection: Any,
    table: Table,
    seed: int,
    plans: dict[str, FanOutPlan],
    pairings: dict[str, PairedPlan],
) -> int:
    """Stream generated rows into the table, by the fastest route the backend has."""
    quote = connection.ops.quote_name
    pk_field = table.model._meta.pk
    columns = [quote(pk_field.column)] + [quote(field.column) for _, field in table.columns()]
    prepare = [pk_field.get_db_prep_save] + [field.get_db_prep_save for _, field in table.columns()]
    rows = (
        tuple(prep(value, connection) for prep, value in zip(prepare, row, strict=True))
        for row in generate_rows(table, seed, plans, pairings)
    )

    guard = partial(refuse_queries, connection, table.model.__name__, table.computation_order())

    if connection.vendor == "postgresql":
        return _copy(connection, table.db_table, columns, rows, guard)
    return _insert(connection, table.db_table, columns, rows, guard)


def _copy(
    connection: Any,
    db_table: str,
    columns: list[str],
    rows: Iterator[tuple[Any, ...]],
    guard: Callable[[], AbstractContextManager[None]],
) -> int:
    """``COPY FROM STDIN``, which is the reason this package can be worth using."""
    statement = f"COPY {connection.ops.quote_name(db_table)} ({', '.join(columns)}) FROM STDIN"
    with connection.cursor() as cursor:
        with connection.wrap_database_errors, cursor.copy(statement) as copy, guard():
            for row in rows:
                copy.write_row(row)
        return int(cursor.rowcount)


def _insert(
    connection: Any,
    db_table: str,
    columns: list[str],
    rows: Iterator[tuple[Any, ...]],
    guard: Callable[[], AbstractContextManager[None]],
) -> int:
    """The portable route, for a backend that has no ``COPY``."""
    placeholders = ", ".join(["%s"] * len(columns))
    statement = (
        f"INSERT INTO {connection.ops.quote_name(db_table)} "
        f"({', '.join(columns)}) VALUES ({placeholders})"
    )
    loaded = 0
    with connection.cursor() as cursor:
        while True:
            with guard():
                chunk = list(islice(rows, _INSERT_CHUNK))
            if not chunk:
                break
            cursor.executemany(statement, chunk)
            loaded += len(chunk)
    return loaded


def _analyze(connection: Any, db_table: str) -> None:
    """Populate the statistics the planner reads."""
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(f"ANALYZE {connection.ops.quote_name(db_table)}")
