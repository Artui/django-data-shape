"""Turning a declaration into a database the planner can reason about."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from functools import partial
from itertools import islice
from typing import Any, cast

from django.core.management.color import no_style
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import Model

from django_data_shape.apply_statistics_targets import apply_statistics_targets
from django_data_shape.build_result import BuildResult
from django_data_shape.check_invariants import check_invariants
from django_data_shape.fan_out import FanOut
from django_data_shape.fan_out_plan import FanOutPlan
from django_data_shape.generate_rows import generate_rows
from django_data_shape.invalid_shape import InvalidShape
from django_data_shape.order_tables import order_tables
from django_data_shape.projection import Projection
from django_data_shape.refuse_queries import refuse_queries
from django_data_shape.require_postgres import require_postgres
from django_data_shape.resolve_fan_out import resolve_fan_out
from django_data_shape.shape import Shape
from django_data_shape.shape_not_empty import ShapeNotEmpty
from django_data_shape.table import Table
from django_data_shape.table_result import TableResult

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
    :func:`~django_data_shape.apply_statistics_targets.apply_statistics_targets`
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

    A :class:`~django_data_shape.projection.Projection` sits in the same loop
    rather than in a pass of its own, and where it sits is decided by
    :func:`~django_data_shape.order_tables.order_tables` like everything else:
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
    # One transaction around every table. Without it a shape whose second table
    # fails leaves the first committed and analyzed, and the natural next action
    # -- fix the shape, run it again -- fails on a duplicate key rather than on
    # the original problem.
    with transaction.atomic(using=using):
        # Parents first. Not because the database insists -- Django's foreign
        # keys are deferred, so any order commits -- but because a fan-out reads
        # its parent's real keys and a projection selects from whole tables, and
        # a table with no rows yet has neither.
        for table in order_tables(shape.tables):
            _require_empty(connection, table.db_table)
            # Before the rows rather than beside the ANALYZE, for two reasons
            # that point the same way. A target changed after ANALYZE has run
            # does nothing until the next one, so it has to be in place before
            # the statistics are gathered; and the refusal it can raise is one
            # nobody wants to pay a two-million-row load to hear.
            apply_statistics_targets(connection, table)
            # The only thing that differs between the two kinds of declaration
            # is where the rows come from. Everything after this branch is a
            # property of a table that now has rows rather than of how they got
            # there, which is why the tail below is shared rather than repeated:
            # a projected table needs its sequence moved and its statistics
            # gathered for exactly the reasons a loaded one does.
            if isinstance(table, Projection):
                loaded = _project(connection, table, shape.seed)
            else:
                plans = _resolve(connection, table, shape.seed)
                loaded = _load(connection, table, shape.seed, plans)
            _reset_sequence(connection, table.model)
            _analyze(connection, table.db_table)
            results.append(TableResult(table=table.db_table, rows=loaded))
        # The second of the three nets, and the only one that covers a rule the
        # schema does not state. Inside the transaction and after every table,
        # for two separate reasons: a rule may span tables, so it cannot run
        # until the last one is in; and a violation has to roll the load back,
        # because a database full of impossible data makes every later assertion
        # pass or fail for a reason unrelated to the code under test.
        check_invariants(connection, shape.invariants)
    return BuildResult(tables=tuple(results))


def _project(connection: Any, projection: Projection, seed: int) -> int:
    """Fill a table from tables already built, with one ``INSERT ... SELECT``.

    No guard around it, unlike generation: a projection runs no code the caller
    supplied, so there is nothing here that could reach the database when it
    should not. The statement *is* the database call.

    Inserting nothing is refused rather than reported. A projection that comes
    out empty has not built a smaller world; it has left a declared table out of
    the database entirely, and every test reading it then passes or fails for a
    reason unrelated to the code. This is the same class of refusal
    :class:`~django_data_shape.derivations.given.Given` makes during a load --
    one of the few that cannot happen at declaration time, because what it
    depends on lives in the other tables rather than in the declaration.
    """
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


def _resolve(connection: Any, table: Table, seed: int) -> dict[str, FanOutPlan]:
    """Partition each declared relation over the parent keys that exist."""
    parent_fields = table.parent_fields()
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
            parent_fields.get(name, ()),
        )
    return plans


def _require_empty(connection: Any, db_table: str) -> None:
    """Refuse to build on top of rows that are already there.

    The keys this package assigns start at 1 every time, so a second build over
    the same table collides on the primary key. That surfaced as a bare
    UniqueViolation naming an index, which tells the reader nothing about what
    they did or what to do instead.

    The message names the likely cause and not only the remedy, because the
    first consumer met this from a direction the remedy does not fit: a
    session-scoped ``shape_fixture`` and a scaled world pointed at one model.
    The rows are then real, correct, and put there by a fixture the failing test
    never mentions -- so "empty the table first" reads as advice about somebody
    else's data.
    """
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT EXISTS (SELECT 1 FROM {connection.ops.quote_name(db_table)})")
        row = cursor.fetchone()
    if row[0]:
        raise ShapeNotEmpty(
            f"{db_table} already holds rows, and this package assigns primary keys from 1, "
            "so building over them would collide. If nothing in the test wrote them, the usual "
            "cause is a world that was already there: a session-scoped shape_fixture over this "
            "model holds its rows for the whole run, and a scaled world cannot build over them. "
            "Give the two different models, or empty this table first."
        )


# The connection is typed loosely on purpose. ``cursor.copy()`` is psycopg 3's
# API reached through Django's cursor wrapper, and it appears on no Django base
# class, so annotating the real wrapper type would mean asserting the checker
# out of the way on every line that uses it.
def _load(connection: Any, table: Table, seed: int, plans: dict[str, FanOutPlan]) -> int:
    """Stream generated rows into the table, by the fastest route the backend has.

    Each declared value is passed through its field's ``get_db_prep_save``
    first, and that is not a formality. Skipping it is how a naive datetime got
    written five hours away from where ``save()`` would have put it under a
    non-UTC ``TIME_ZONE`` -- silently, on the exact column ``Sequential`` exists
    to make realistic -- and how a ``JSONField`` failed to load at all. The
    generator stays backend-neutral because the preparation happens here rather
    than inside it, and that is also why the preparation sits **above** the
    branch below: a value is prepared by its field for its connection, which is
    the same work whichever statement carries it.

    Returns the number of rows the database actually took, not the number
    declared. They are the same today and stop being so once deduplicated
    many-to-many edges arrive.
    """
    quote = connection.ops.quote_name
    pk_field = table.model._meta.pk
    columns = [quote(pk_field.column)] + [quote(field.column) for _, field in table.columns()]
    # The primary key is prepared like every other value. It did not need to be
    # while keys were always integers; a UUID key does, and a strategy the
    # caller wrote could return anything its column accepts.
    prepare = [pk_field.get_db_prep_save] + [field.get_db_prep_save for _, field in table.columns()]
    rows = (
        tuple(prep(value, connection) for prep, value in zip(prepare, row, strict=True))
        for row in generate_rows(table, seed, plans)
    )

    # The guard is built here rather than inside each route because it belongs
    # to generation, and generation is what both routes share. This package may
    # call the caller's code -- a derivation, a distribution, a key strategy --
    # and that code may not call the database; the wrapper is what turns that
    # sentence from a convention into a refusal.
    # A factory rather than one context manager: Django's execute_wrapper is a
    # generator-based context manager, so it is good for exactly one entry, and
    # the portable route below enters one per chunk.
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
    """``COPY FROM STDIN``, which is the reason this package can be worth using.

    ``bulk_create`` is the obvious alternative and is roughly an order of
    magnitude too slow at the row counts that make a plan meaningful. ``COPY``
    is also why the generator yields tuples rather than model instances: there
    is no instance to build, and no ``save`` to run.

    The whole streaming loop sits inside the guard, and it can because
    ``cursor.copy()`` is reached through Django's cursor wrapper by attribute
    rather than through ``execute``. Nothing this package does in here is a
    wrapped statement, so anything that is came from the caller.
    """
    statement = f"COPY {connection.ops.quote_name(db_table)} ({', '.join(columns)}) FROM STDIN"
    with connection.cursor() as cursor:
        # ``copy`` is not in Django's WRAP_ERROR_ATTRS, so without this a
        # Postgres error escapes as a raw psycopg exception: the caller cannot
        # catch django.db.IntegrityError, and -- worse -- an enclosing atomic
        # block never learns it needs a rollback, so the next query inside it
        # fails with "current transaction is aborted" instead of a Django error.
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
    """The portable route, for a backend that has no ``COPY``.

    Reached only when the caller said it does not require planner statistics,
    which is the honest shape of the trade: this writes real rows in real
    cardinality and buys nothing at all for a plan. It is slower than ``COPY``
    and that is the wrong thing to worry about at the sizes it is for --
    measured on SQLite, the insert costs about 1.6 ms per thousand rows against
    8 ms to generate them, so the load is not what a growth assertion pays for.

    Chunked rather than handed the whole iterator, because ``executemany``
    materialises what it is given: streaming into ``COPY`` is the property this
    package is built on, and a portable path that quietly held a million tuples
    in memory would be a different bargain from the one above.

    The chunk is generated inside the guard and written outside it, which is the
    one place the two routes differ: here the load *is* a wrapped statement, so
    a guard spanning both would refuse this package's own insert.
    """
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


def _reset_sequence(connection: Any, model: type[Model]) -> None:
    """Move the identity sequence past the keys this package just assigned.

    Skipping this is the first bug the design invites: rows exist at ids 1..N
    while the sequence still starts at 1, so the very first ``objects.create()``
    inside a test raises ``IntegrityError`` on a primary key that is already
    taken. Django's own backend operation is used rather than a hand-written
    ``setval`` because it already knows how the column's sequence is named.
    """
    with connection.cursor() as cursor:
        for statement in connection.ops.sequence_reset_sql(no_style(), [model]):
            cursor.execute(statement)


def _analyze(connection: Any, db_table: str) -> None:
    """Populate the statistics the planner reads.

    Rows alone change nothing: without this the planner falls back to a default
    selectivity and commits to it, which is how a two-million-row table gets
    bitmap-scanned through an index for a value matching 98% of it. Measured at
    81 ms on that table, because ``ANALYZE`` samples rather than scans.

    Runs for a projected table exactly as it does for a loaded one. That is not
    a detail: rows arriving by ``INSERT ... SELECT`` are as invisible to the
    planner as rows arriving by ``COPY``, and a projection is the route most
    likely to feel like it inherited its statistics from the tables it copied.
    It does not -- statistics describe a table, not a query.

    Nothing is gathered on another backend, and SQLite is the case worth being
    explicit about: it has an ``ANALYZE`` of its own and running it is one line.
    It is deliberately not run. Plan realism on SQLite is out of this package's
    scope -- support the generation, refuse the pretence -- and a table with
    ``sqlite_stat1`` behind it would be this package claiming, in the only way a
    library can, that the plan over it means something.
    """
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(f"ANALYZE {connection.ops.quote_name(db_table)}")
