from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, NamedTuple, cast
from weakref import WeakKeyDictionary

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import Model

from django_data_shape.declaration.projection import Projection
from django_data_shape.declaration.shape import Shape
from django_data_shape.declaration.table import Table
from django_data_shape.keys.is_disjoint import is_disjoint
from django_data_shape.keys.key_strategy import KeyStrategy
from django_data_shape.keys.uuid_keys import UuidKeys
from django_data_shape.loading.build import build
from django_data_shape.scaling.scaled_shape import scaled_shape
from django_data_shape.scaling.shape_referenced import ShapeReferenced
from django_data_shape.utils import field_stream, primary_key_field, reset_sequence

# How many worlds are open on each connection, which is what a world's
# Disjoint key stream is salted with on top of _WORLD_KEYS: a world opened
# inside another over the same Disjoint table builds beside the outer world's
# rows, and from the outer world's stream it would make the outer world's keys.
# Keyed by the connection object rather than by the alias, because Django gives
# each thread a connection of its own under one alias, and held weakly so that
# a connection Django drops takes its count with it.
_OPEN_WORLDS: WeakKeyDictionary[Any, int] = WeakKeyDictionary()

# What a world's Disjoint key stream differs from build()'s by, for the same
# seed and table, before the depth is mixed in. A fixed 64-bit constant derived
# the way every stream here is, from a name, rather than a number picked by
# hand. It has high bits set, so XORed with any depth a process could reach it
# is never zero -- the world's stream is never build()'s -- and two depths
# never share a stream either. A session world would need the world's seed
# XORed with this constant to meet it, which nobody picks.
_WORLD_KEYS = field_stream(0, "scaled_world", ":key")


@contextmanager
def scaled_world(shape: Shape, factor: int, *, using: str = DEFAULT_DB_ALIAS) -> Generator[int]:
    """Build ``shape`` at ``factor``, run the caller's block, then undo it.

    The implementation of :class:`~django_data_shape.scaling.scale_protocol.ScaleProtocol`
    for a project that uses this package. Bound to a shape it *is* one::

        world = functools.partial(scaled_world, Shape(Table(Order, rows=100)))

        for factor in (1, 10):
            with world(factor) as rows:
                ...

    Yields the number of rows the world holds, which is what the database took
    rather than what the declaration asked for -- the two are the same today and
    stop being so once deduplicated many-to-many edges arrive, and a growth
    curve annotated with a number nothing achieved would be worse than one
    annotated with none.

    **It does not require planner statistics, and that is what makes it
    portable.** A growth assertion counts queries, and a query count is an ORM
    property that means the same on any backend -- so this builds wherever there
    are rows to build, using ``COPY`` and ``ANALYZE`` where the backend has them
    and plain inserts where it does not. Nothing about a plan is claimed on a
    backend that cannot support the claim, which is the same line this package
    draws everywhere: generation and cardinality are backend-neutral, planner
    realism is not. A *plan* assertion still belongs behind
    :func:`~django_data_shape.fixtures.skip_unless_postgres.skip_unless_postgres`.

    **Open a query capture inside the block, never around it.** Building a world
    emits statements of its own, and a capture wrapped around ``world(factor)``
    counts them along with the block's. On PostgreSQL that is mild and **fixed**:
    nineteen statements for a two-table shape over empty tables at every factor,
    because one ``COPY`` loads a table however many rows it carries, and
    everything else a world emits -- the read of which declared tables hold
    rows, the emptiness check, the statistics-target read, the parent key read,
    the sequence resets, the ``ANALYZE`` and the savepoints -- is counted per
    table or per world, never per row. Over a session world declaring the same
    tables, emptying them first adds five more where another table references
    them, four where none does -- the read of what references them, the check
    that none of it holds rows (which has nothing to read when nothing
    references them), the two statements firing pending foreign-key checks, and
    the ``TRUNCATE`` -- and that too is the same at every factor.

    That nineteen is counted with ``CaptureQueriesContext`` -- what
    ``django_assert_num_queries`` reads -- inside a non-transactional ``django_db``
    test. **Both halves of that sentence move the number**: the same shape counted
    through ``execute_wrapper``, which is what a capture built on that hook sees,
    is seventeen, because ``COPY`` reaches the query log by a route the wrapper
    does not; and a ``transaction=True`` test drops one more savepoint. So do not
    read the absolute figure as a constant of this package.
    **What is invariant, and what the tests below pin, is the shape of each: fixed
    on PostgreSQL whatever the factor, growing off it.** Off PostgreSQL it is
    neither: the inserts are ordinary statements, one per thousand rows, **so the
    captured count grows with the factor** -- and a growth assertion measuring
    from outside the block would read the loader's own curve as its subject's.

    Both halves of that are pinned by tests rather than left as prose, because a
    measurement in a docstring is the first thing to rot and the consumer this
    matters to cannot check it without taking the dependency the protocol exists
    to avoid. The number above was already wrong once, for exactly that reason.

    **The teardown is a rollback, not a delete.** Building inside a transaction
    and rolling it back at the end restores exactly the state the block started
    from, which matters twice: nothing destructive this package does survives
    the block -- the emptying below included -- and inside a pytest-django
    ``db`` test the rollback is to a savepoint, so it costs nothing and leaves
    the enclosing test transaction usable afterwards. Outside one it is an
    ordinary transaction rollback, so the same code is correct in both places.

    **The declared tables are emptied first, and nothing else is: no statement a
    world issues changes a table its shape does not declare,** not even for the
    life of the block. A declared table that already holds rows -- a session
    world's, or the caller's own -- is emptied, inside the transaction the
    block rolls back, so they come back afterwards. A table whose keys are
    :class:`~django_data_shape.keys.disjoint.Disjoint` -- a strategy that
    implements the protocol and answers yes, the reading ``build`` makes too
    -- is not emptied, and the world builds beside the rows already there,
    with keys of its own (below) -- unless it has a foreign key
    into a declared table that is being emptied, directly or through another
    such table, and holds rows. Then it is emptied too: its rows are declared
    rows, and they cannot outlive the parents they point at. Those foreign keys
    are read from the models, not the database, so a
    ``ForeignKey(db_constraint=False)`` pulls a table in although the refusal
    below does not see one -- and a row referencing that table is then
    refused, where the database's own keys would have left nothing to refuse.

    **So a session world under a scaled world over the same graph needs no
    arrangement**, a declared ``Disjoint`` table holding the session's rows
    included -- one pointing at nothing the world empties, such as a
    UUID-keyed root, or any table of a graph keyed by UUIDs throughout. The
    world leaves those rows in place and untouched, builds beside them, and
    they are back as they were afterwards. Its keys are not theirs.
    ``UuidKeys`` and ``Md5Keys`` make each key from the row and a stream
    derived from the seed and the table, and scaling keeps the seed, so every
    ``Disjoint`` table a world builds is handed a stream of the world's own,
    never the one ``build()`` uses for the same seed and table: a world's keys
    there are not the keys ``build()`` gives the same declaration. The stream does not
    depend on the factor, so row ``i`` has one key at every factor, as an
    integer key does. A world opened inside another over the same table draws
    from another stream again, and builds beside the outer world's rows the
    same way. A table fanned out over such a table draws parents from the
    session's rows as well as the world's, whatever the seed.

    What the world's statements set off is another matter. A row-level
    ``DELETE`` trigger on a declared table runs when the world empties that
    table by ``DELETE`` (see below), where ``TRUNCATE`` fires none: an audit
    trigger writing into an undeclared table writes there, and that write is
    rolled back with the rest of the block; a ``BEFORE DELETE`` trigger that
    returns null keeps its rows, and the build then refuses the table with
    :class:`~django_data_shape.loading.shape_not_empty.ShapeNotEmpty`.

    **Where a row the world did not make references a row it must remove, the
    world refuses**, with
    :class:`~django_data_shape.scaling.shape_referenced.ShapeReferenced`, before
    it removes anything. Removing the row would leave the reference pointing at
    nothing or take the referencing row with it, and either would change a
    table the shape does not declare. The message names each reference as
    ``referencing_table.column -> declared_table`` and the ways out: declare
    the referencing table too, so its rows are the world's; give the declared
    table ``Disjoint`` keys -- offered only where that would end the refusal,
    so where every table named can take them, which an integer primary key
    cannot, and none has a foreign key into another table being emptied, which
    would pull it straight back in; or do not create those rows in that test. A reference left null is not one. Only a foreign key the
    database enforces is seen: a ``ForeignKey(db_constraint=False)`` and a
    ``GenericForeignKey`` are invisible to the refusal, so a row holding one
    is left pointing at whatever the world puts under that key. PostgreSQL's
    catalogue answers the refusal there and Django's introspection everywhere
    else, which reports each column of a composite foreign key on its own, so
    off PostgreSQL a composite key counts as a reference when any of its
    columns is set rather than all of them. Django never creates one.

    **That order is what keeps a ``DELETE`` inside the declaration.** A
    ``DELETE`` can reach past the table it names: a database-level ``ON
    DELETE`` -- ``CASCADE``, ``SET NULL`` or ``SET DEFAULT``, which Django 6.1
    creates for ``DB_CASCADE`` and its siblings -- acts on every row
    referencing a row the statement removes. The refusal runs first, over
    every key the database enforces into a declared table being emptied, so
    when the first ``DELETE`` runs no row outside the tables being emptied
    references one of their rows, and such a key has nothing outside them to
    act on (``test_a_cascading_key_from_an_undeclared_table_is_refused_before_anything_is_removed``).
    What it can still reach is rows of the tables being emptied, which the
    world removes anyway
    (``test_a_declared_child_under_a_cascading_key_is_emptied_with_its_parent``).
    A key the database does not enforce carries no ``ON DELETE`` to follow.

    On PostgreSQL the emptying is one ``TRUNCATE`` when nothing outside the
    declaration holds rows, which is the case a session world under a scaled
    world over the same graph meets every time. It lists the declared tables
    holding rows and every table referencing them, transitively -- all empty,
    so nothing outside the declaration loses a row -- and
    ``CASCADE`` is never used: ``CASCADE`` follows foreign keys by schema,
    transitively, whatever the rows hold, and in a schema where a chain of keys
    leads from a declared child back to its own parent it empties the parent
    too. Before the ``TRUNCATE`` the foreign-key checks still pending from the
    caller's own writes are fired. Django creates PostgreSQL foreign keys
    ``DEFERRABLE INITIALLY DEFERRED``, so a child row written earlier in the
    transaction -- by a factory's ``SubFactory``, say -- leaves its check
    queued, and PostgreSQL refuses to truncate a table with pending trigger
    events. Firing them changes only *when* they run: a row that genuinely
    violates a constraint raises ``IntegrityError`` on the way into the world,
    naming the constraint, rather than whenever the enclosing transaction next
    checks. Otherwise -- some undeclared table holds rows, none of them
    referencing a declared one -- and off PostgreSQL always, each declared
    table holding rows is emptied by one ``DELETE``, children first. On
    PostgreSQL the pending checks are fired after those ``DELETE`` statements,
    so that both routes enter the block alike: a row outside the declaration
    that breaks a constraint raises ``IntegrityError`` on the way in either
    way. PostgreSQL also refuses the build's ``ALTER TABLE ... SET STATISTICS``
    on a table with checks still queued -- and each row a ``DELETE`` removes
    from a referenced table queues one -- which the build answers itself: it
    fires the pending checks before setting a declared statistics target,
    whether or not the world emptied anything.

    The two routes part on a row in a declared table that breaks a constraint,
    such as an orphan the caller wrote. The ``TRUNCATE`` route fires its check
    before the statement, so it raises ``IntegrityError``. The ``DELETE`` route
    removes the row first, PostgreSQL skips a queued check whose row is gone,
    and the world builds; the orphan is back after the block.

    Firing the checks ends with ``SET CONSTRAINTS ALL DEFERRED``, on either
    route, so from there to the end of the block every deferrable constraint
    is deferred, one declared ``INITIALLY IMMEDIATE`` included, and the
    caller's block runs under that. The mode is transaction state, and the
    rollback that ends the block restores the caller's.

    One thing the rollback does not undo, because the database will not: an
    identity sequence moved past the keys a build assigned stays moved, since
    ``setval`` is not transactional. Nothing here reads it -- keys are assigned,
    not drawn -- so the only visible effect is that a row created by the ORM
    after a world is torn down gets a larger id than it otherwise would.
    """  # noqa: E501
    scaled = scaled_shape(shape, factor)
    connection = connections[using]
    depth = _OPEN_WORLDS.get(connection, 0)
    world = _with_world_keys(scaled, depth)
    _OPEN_WORLDS[connection] = depth + 1
    try:
        with transaction.atomic(using=using):
            _empty_declared_tables(world, using)
            result = build(world, using=using, require_statistics=False)
            yield result.rows
            transaction.set_rollback(True, using=using)
    finally:
        _OPEN_WORLDS[connection] = depth
        # setval is not transactional, so sequences reset after the rollback, never inside it.
        _reset_declared_sequences(scaled, using)


class _Reference(NamedTuple):
    """One foreign key into a table the world is about to empty.

    ``sql`` names the referencing table for a statement, ``table`` for a message."""

    sql: str
    table: str
    columns: tuple[str, ...]
    declared: str


def _empty_declared_tables(shape: Shape, using: str) -> None:
    """Remove the rows of the tables ``shape`` declares, and nothing else."""
    connection = connections[using]
    if all(_keeps_its_keys(table) for table in shape.tables):
        return
    with connection.cursor() as cursor:
        held = _exists(
            cursor, [(connection.ops.quote_name(table.db_table), "") for table in shape.tables]
        )
        holding = {table.db_table for table, holds in zip(shape.tables, held, strict=True) if holds}
        candidates = _candidates(shape, holding)
        if not candidates:
            return
        if connection.vendor == "postgresql":
            closure, references = _referencing_closure(connection, cursor, candidates)
            if not any(_exists(cursor, [(name, "") for name in closure])):
                connection.check_constraints()
                listed = [connection.ops.quote_name(name) for name in candidates] + closure
                cursor.execute(f"TRUNCATE {', '.join(listed)}")
                return
        else:
            references = _referencing_tables(connection, cursor, candidates)
        _refuse_held_references(connection, cursor, references, shape, candidates)
        for name in reversed(candidates):
            cursor.execute(f"DELETE FROM {connection.ops.quote_name(name)}")
        if connection.vendor == "postgresql":
            connection.check_constraints()


def _keeps_its_keys(table: Table | Projection) -> bool:
    """Whether ``table`` builds beside rows already there rather than being emptied."""
    return is_disjoint(getattr(table, "keys", None))


def _with_world_keys(shape: Shape, depth: int) -> Shape:
    """``shape`` with every Disjoint table drawing its keys from the world's own stream."""
    salt = _WORLD_KEYS ^ depth
    return Shape(
        *(
            Table(
                table.model,
                rows=table.rows,
                fields=dict(table.fields),
                keys=_WorldKeys(table.keys, salt),
                statistics=dict(table.statistics),
            )
            if isinstance(table, Table) and is_disjoint(table.keys)
            else table
            for table in shape.tables
        ),
        seed=shape.seed,
        invariants=tuple(shape.invariants),
    )


class _WorldKeys:
    """A Disjoint strategy's keys, drawn from a stream of the world's own.

    Still a Disjoint strategy, answering what the one it wraps answers."""

    def __init__(self, keys: KeyStrategy, salt: int) -> None:
        self._keys = keys
        self._salt = salt

    def key_for(self, row: int, stream: int) -> object:
        return self._keys.key_for(row, stream ^ self._salt)

    def is_disjoint_from_existing_rows(self) -> bool:
        return is_disjoint(self._keys)


def _candidates(shape: Shape, holding: set[str]) -> list[str]:
    """The declared tables to empty, in declaration order, given which hold rows."""
    tables = [table for table in shape.tables if table.db_table in holding]
    joined = {table.db_table for table in tables if not _keeps_its_keys(table)}
    while (
        joining := {table.db_table for table in tables if _foreign_key_targets(table) & joined}
        - joined
    ):
        joined |= joining
    return [table.db_table for table in shape.tables if table.db_table in joined]


def _foreign_key_targets(table: Table | Projection) -> set[str]:
    """The tables ``table``'s model holds a foreign key into, by ``db_table``."""
    return {
        cast("type[Model]", field.related_model)._meta.db_table
        for field in table.model._meta.concrete_fields
        if field.is_relation
    }


def _exists(cursor: Any, probes: list[tuple[str, str]]) -> list[bool]:
    """Whether each ``(table, condition)`` matches a row, in one statement."""
    if not probes:
        return []
    cursor.execute(
        "SELECT "
        + ", ".join(f"EXISTS (SELECT 1 FROM {table}{condition})" for table, condition in probes)
    )
    return [bool(found) for found in cursor.fetchone()]


# Every foreign key into a table reachable from the candidates by following
# foreign keys backwards, transitively -- the set TRUNCATE ... CASCADE takes,
# computed the way PostgreSQL computes it, from the constraints rather than the
# rows. A key from a candidate is left out: a candidate's own rows are the
# world's, and a reference between two of them is removed with both. The
# referenced table comes back as its position among the candidates, or null
# for a table that is not one, so the caller can name it exactly as it was
# declared: regclass text quotes and qualifies a name wherever it must, and a
# declared table is matched to its declaration by that name.
_REFERENCING_CLOSURE = """
WITH RECURSIVE reached (relid) AS (
    SELECT unnest(%(candidates)s::regclass[])::oid
    UNION
    SELECT foreign_key.conrelid
    FROM pg_constraint AS foreign_key
    JOIN reached ON foreign_key.confrelid = reached.relid
    WHERE foreign_key.contype = 'f'
)
SELECT
    foreign_key.conrelid::regclass::text,
    array_position(%(candidates)s::regclass[], foreign_key.confrelid::regclass),
    ARRAY(
        SELECT attribute.attname
        FROM unnest(foreign_key.conkey) WITH ORDINALITY AS key (attnum, position)
        JOIN pg_attribute AS attribute
          ON attribute.attrelid = foreign_key.conrelid AND attribute.attnum = key.attnum
        ORDER BY key.position
    )
FROM pg_constraint AS foreign_key
JOIN reached ON foreign_key.confrelid = reached.relid
WHERE foreign_key.contype = 'f'
  AND NOT foreign_key.conrelid = ANY (%(candidates)s::regclass[])
ORDER BY 1, foreign_key.confrelid::regclass::text, foreign_key.conname
"""


def _referencing_closure(
    connection: Any, cursor: Any, candidates: list[str]
) -> tuple[list[str], list[_Reference]]:
    """The tables a ``TRUNCATE`` of ``candidates`` must list, and the keys into them."""
    cursor.execute(
        _REFERENCING_CLOSURE,
        {"candidates": [connection.ops.quote_name(name) for name in candidates]},
    )
    keys = cursor.fetchall()
    closure = list(dict.fromkeys(referencing for referencing, *_ in keys))
    references = [
        _Reference(referencing, referencing, tuple(columns), candidates[position - 1])
        for referencing, position, columns in keys
        if position is not None
    ]
    return closure, references


def _referencing_tables(connection: Any, cursor: Any, candidates: list[str]) -> list[_Reference]:
    """Every foreign key into a candidate, off PostgreSQL, through introspection."""
    introspection = connection.introspection
    references: list[_Reference] = []
    for name in introspection.table_names(cursor):
        if name in candidates:
            continue
        for column, relation in introspection.get_relations(cursor, name).items():
            if relation[1] in candidates:
                references.append(
                    _Reference(connection.ops.quote_name(name), name, (column,), relation[1])
                )
    return references


def _refuse_held_references(
    connection: Any, cursor: Any, references: list[_Reference], shape: Shape, candidates: list[str]
) -> None:
    """Raise :class:`ShapeReferenced` if any row holds one of ``references``."""
    quote = connection.ops.quote_name
    held = _exists(
        cursor,
        [
            (
                reference.sql,
                " WHERE "
                + " AND ".join(f"{quote(column)} IS NOT NULL" for column in reference.columns),
            )
            for reference in references
        ],
    )
    found = [reference for reference, holds in zip(references, held, strict=True) if holds]
    if not found:
        return
    named = "; ".join(
        f"{reference.table}.{', '.join(reference.columns)} -> {reference.declared}"
        for reference in found
    )
    referencing = " and ".join(dict.fromkeys(reference.table for reference in found))
    names = list(dict.fromkeys(reference.declared for reference in found))
    declared = " and ".join(names)
    by_name = {table.db_table: table for table in shape.tables}
    emptied = set(candidates)
    disjoint = (
        f"give {declared} Disjoint keys (UuidKeys or Md5Keys), so the world builds beside the "
        "rows already there instead of emptying the table; "
        if all(
            _disjoint_keys_fit(by_name[name])
            and not _foreign_key_targets(by_name[name]) & (emptied - {name})
            for name in names
        )
        else ""
    )
    raise ShapeReferenced(
        f"A scaled world cannot empty {declared} without changing a table its shape does not "
        f"declare: rows it did not make reference the rows it would remove ({named}). A world "
        "removes the rows of the tables it declares and nothing else, so it refuses rather than "
        "leave those references pointing at nothing or take their rows along. "
        f"Declare {referencing} in the shape too, so those rows are the world's; {disjoint}"
        "or do not create those rows in this test."
    )


# A key of the kind both Disjoint strategies make. UuidKeys and Md5Keys each
# return a uuid.UUID -- different digests, the same type -- which the loader
# hands to the primary key's get_db_prep_save, so one stands for both. Its text
# form is what is checked, because to_python's contract is to accept a string
# and to refuse one it cannot parse with a ValidationError, where a field handed
# an object it never expected may raise anything.
_DISJOINT_KEY = str(UuidKeys().key_for(0, 0))


def _disjoint_keys_fit(table: Table | Projection) -> bool:
    """Whether giving ``table`` Disjoint keys would take it out of the emptying."""
    keys = getattr(table, "keys", None)
    if keys is None or is_disjoint(keys):
        return False
    field = primary_key_field(table.model)
    if field.is_relation:
        return False
    try:
        field.clean(_DISJOINT_KEY, None)
    except (ValidationError, ValueError, TypeError):
        return False
    return True


def _reset_declared_sequences(shape: Shape, using: str) -> None:
    """Point every declared table's sequence at the rows it now holds."""
    connection = connections[using]
    for table in shape.tables:
        reset_sequence(connection, table.model)
