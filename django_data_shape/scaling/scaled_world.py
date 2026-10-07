"""One world at one scale factor, undone when the block ends."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, NamedTuple

from django.db import DEFAULT_DB_ALIAS, connections, transaction

from django_data_shape.declaration.shape import Shape
from django_data_shape.keys.disjoint import Disjoint
from django_data_shape.loading.build import build
from django_data_shape.scaling.scaled_shape import scaled_shape
from django_data_shape.scaling.shape_referenced import ShapeReferenced
from django_data_shape.utils import reset_sequence


@contextmanager
def scaled_world(shape: Shape, factor: int, *, using: str = DEFAULT_DB_ALIAS) -> Iterator[int]:
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
    tables, emptying them first adds five more -- the read of what references
    them, the check that none of it holds rows, the two statements firing
    pending foreign-key checks, and the ``TRUNCATE`` -- and that too is the same
    at every factor.

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

    **The declared tables are emptied first, and nothing else is: a world never
    changes a table its shape does not declare,** not even for the life of the
    block. A declared table that already holds rows -- a session world's, or
    the caller's own -- is emptied, inside the transaction the block rolls
    back, so they come back afterwards. A table whose keys are
    :class:`~django_data_shape.keys.disjoint.Disjoint` is not emptied at all,
    and the world builds beside the rows already there.

    **Where a row the world did not make references a row it must remove, the
    world refuses**, with
    :class:`~django_data_shape.scaling.shape_referenced.ShapeReferenced`, before
    it removes anything. Removing the row would leave the reference pointing at
    nothing or take the referencing row with it, and either would change a
    table the shape does not declare. The message names each reference as
    ``referencing_table.column -> declared_table`` and the three ways out:
    declare the referencing table too, so its rows are the world's; give the
    declared table ``Disjoint`` keys; or do not create those rows in that test.
    A reference left null is not one. The refusal is the same on every
    backend: PostgreSQL's catalogue answers it there, Django's introspection
    everywhere else.

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
    table holding rows is emptied by one ``DELETE``, children first, and nothing
    is fired first.

    One thing the rollback does not undo, because the database will not: an
    identity sequence moved past the keys a build assigned stays moved, since
    ``setval`` is not transactional. Nothing here reads it -- keys are assigned,
    not drawn -- so the only visible effect is that a row created by the ORM
    after a world is torn down gets a larger id than it otherwise would.
    """
    scaled = scaled_shape(shape, factor)
    try:
        with transaction.atomic(using=using):
            _empty_declared_tables(scaled, using)
            result = build(scaled, using=using, require_statistics=False)
            yield result.rows
            # Rolling back on the way out rather than raising and swallowing
            # an exception to get there: set_rollback is the supported way to
            # leave an atomic block without keeping it. An exception from the
            # caller's block never reaches this line and does not need to --
            # atomic rolls back for exactly that case already.
            transaction.set_rollback(True, using=using)
    finally:
        # After the rollback, never inside it. The rows are back and the
        # sequences are not: ``setval`` is not transactional, so the counter
        # still holds whatever the scaled build moved it to -- and a scaled
        # world is usually *smaller* than a session world built over, which
        # leaves it pointing at ids that have just come back. The next
        # ``objects.create()`` then collides with a row the failing test never
        # wrote, in a later test, which is about as far from the cause as a
        # symptom gets.
        #
        # Recomputed from the rows rather than restored from a number captured
        # on the way in, because the reset reads ``max(pk)`` of whatever is
        # actually there: it is then correct in both directions, and correct
        # too when the caller's block raised partway through the build.
        _reset_declared_sequences(scaled, using)


class _Reference(NamedTuple):
    """One foreign key into a table the world is about to empty.

    ``sql`` is how a statement names the referencing table and ``table`` is how
    a message does. They differ only off PostgreSQL, where the name comes from
    introspection bare and has to be quoted; PostgreSQL's catalogue hands back
    a name already quoted and qualified wherever the table needs it.
    """

    sql: str
    table: str
    columns: tuple[str, ...]
    declared: str


def _empty_declared_tables(shape: Shape, using: str) -> None:
    """Remove the rows of the tables ``shape`` declares, and nothing else.

    A world at a factor is the declared shape at that size and nothing else, so
    rows already in those tables are not this world's -- and emptying them is
    what lets a scaled world be built over a session world, which is the one
    composition of this package's two pytest surfaces that an application with a
    single model graph needs and could not have.

    **Nothing is snapshotted, and nothing needs to be.** This runs inside the
    atomic block ``scaled_world`` already rolls back, so whatever was there
    comes back when the block ends -- the session world included.

    Emptying rather than refusing is safe *only* here, and that is why it lives
    in this function and not in ``build``. A bare ``build()`` keeps its refusal,
    because it has no transaction of its own to undo and would be destroying
    rows for good.

    A table whose keys are :class:`~django_data_shape.keys.disjoint.Disjoint`
    is left alone, mirroring the exemption ``build`` makes for the same reason:
    those keys cannot collide with a caller's rows, so the hybrid this package
    documents -- parents made by your code, children made here -- must keep
    working.

    **Only a declared table that holds a row needs emptying** -- a
    *candidate* -- and a world with none issues no statement after the read
    that found them empty, foreign-key checks included
    (``test_a_world_over_empty_tables_issues_no_emptying_statement``).

    **A row outside the declaration that references a candidate is refused**,
    with :class:`~django_data_shape.scaling.shape_referenced.ShapeReferenced`
    naming it, because removing the row it points at would leave it pointing at
    nothing or take it along, and either changes a table the shape does not
    declare. A reference counts only when it comes from a table that is not a
    candidate (``test_declaring_the_fees_too_makes_their_rows_the_worlds``), goes
    into one (``test_a_reference_left_null_is_no_reason_to_refuse``, and the
    ``selected_fee`` assertion in
    ``test_a_fee_the_caller_made_refuses_the_world_naming_its_column``) and has
    every one of its columns set: a row with any of them null references
    nothing (``test_a_reference_left_null_is_no_reason_to_refuse`` for one
    column, ``test_a_composite_reference_with_one_column_null_references_nothing``
    for several). Each of those conditions was mutated out and the test named
    beside it failed, which a branch-coverage gate cannot show: a condition
    deleted from a filter leaves every branch still taken.

    **On PostgreSQL the emptying is one ``TRUNCATE`` when it can be**, because
    that is the case a session world under a scaled world over the same graph
    meets every time. ``TRUNCATE``'s unit is a set of tables closed under
    foreign keys -- PostgreSQL refuses to truncate a table something outside
    the statement references -- so the statement lists every table that
    references a candidate, transitively, read from ``pg_constraint``: the set
    ``CASCADE`` would have taken, named rather than implied. It runs only when
    none of those tables holds a row, so truncating them removes nothing
    (``test_a_world_declaring_a_child_leaves_the_callers_parent_alone``, where
    the caller's club is such a row); and because the list is explicit, a list
    that ever missed a table would be refused by PostgreSQL rather than
    silently widened
    (``test_over_a_session_world_the_emptying_is_one_truncate_naming_what_references_it``).
    Otherwise some referencing table holds rows, and after the refusal check
    the candidates are emptied by ``DELETE`` instead, children before parents
    (``test_the_delete_route_empties_children_before_parents``).

    **Off PostgreSQL the same refusal is made through Django's introspection**,
    which reads every table's foreign keys -- a cost paid only by a world whose
    declared tables already hold rows, since one over empty tables stops at the
    first read. It is the same rule on every backend, so a world never changes
    a table its shape does not declare on any of them, where the ``DELETE``
    alone would have reached one through a database-level ``ON DELETE``, or
    left its rows pointing at keys the world then hands to rows of its own.
    """
    connection = connections[using]
    declared = [
        table.db_table
        for table in shape.tables
        if not isinstance(getattr(table, "keys", None), Disjoint)
    ]
    with connection.cursor() as cursor:
        held = _exists(cursor, [(connection.ops.quote_name(name), "") for name in declared])
        candidates = [name for name, holds in zip(declared, held, strict=True) if holds]
        if not candidates:
            return
        if connection.vendor == "postgresql":
            closure, references = _referencing_closure(connection, cursor, candidates)
            if not any(_exists(cursor, [(name, "") for name in closure])):
                # Fire the foreign-key checks still queued from the caller's
                # own writes first. Django creates PostgreSQL foreign keys
                # DEFERRABLE INITIALLY DEFERRED, so a child row written earlier
                # in this transaction -- by a factory's SubFactory, typically --
                # leaves its check pending until commit, and PostgreSQL refuses
                # to TRUNCATE a table with pending trigger events. A parent row
                # alone queues nothing, which is why only a caller who wrote a
                # child ever saw the refusal.
                #
                # This moves *when* the checks run and nothing else: a row that
                # genuinely violates a constraint now raises here, at world
                # entry and under the constraint's name, rather than whenever
                # the enclosing transaction next checks. check_constraints()
                # ends with SET CONSTRAINTS ALL DEFERRED, which would defer even
                # a constraint declared INITIALLY IMMEDIATE if it outlived the
                # block -- it does not, because the mode is transaction state
                # and rolling back this block's savepoint restores it with
                # everything else. Only on this route: DELETE has no such
                # refusal, and SQLite's version of this call scans every table
                # in the database.
                connection.check_constraints()
                # TRUNCATE is transactional on PostgreSQL, so it rolls back with
                # the rest of the block. No CASCADE: the closure is listed, so
                # PostgreSQL refuses a set that is not closed rather than
                # widening it. RESTART IDENTITY is deliberately omitted, because
                # a sequence reset is not transactional and would leave the
                # counter behind the rows that came back.
                listed = [connection.ops.quote_name(name) for name in candidates] + closure
                cursor.execute(f"TRUNCATE {', '.join(listed)}")
                return
        else:
            references = _referencing_tables(connection, cursor, candidates)
        _refuse_held_references(connection, cursor, references)
        # DELETE, because TRUNCATE cannot remove the declared rows here without
        # taking a table that references them along: its unit is a set closed
        # under references, and one of those tables holds rows that are not
        # this world's. The refusal above has just established that none of
        # those rows references a candidate, so removing every candidate row
        # leaves them untouched and nothing dangling. Children before parents,
        # in the declaration's own order reversed. DELETE runs with trigger
        # events pending, so this route fires nothing first.
        for name in reversed(candidates):
            cursor.execute(f"DELETE FROM {connection.ops.quote_name(name)}")


def _exists(cursor: Any, probes: list[tuple[str, str]]) -> list[bool]:
    """Whether each ``(table, condition)`` matches a row, in one statement.

    One statement however many tables there are, so what a world costs stays
    a property of the world rather than of the schema. No statement at all for
    no probes, which is how a shape declaring only ``Disjoint`` tables reaches
    nothing.
    """
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
# world's, and a reference between two of them is removed with both.
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
    foreign_key.confrelid::regclass::text,
    foreign_key.confrelid = ANY (%(candidates)s::regclass[]),
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
ORDER BY 1, 2, foreign_key.conname
"""


def _referencing_closure(
    connection: Any, cursor: Any, candidates: list[str]
) -> tuple[list[str], list[_Reference]]:
    """The tables a ``TRUNCATE`` of ``candidates`` must list, and the keys into them.

    PostgreSQL only, read in one recursive query over ``pg_constraint``. The
    first half is every table referencing a candidate, transitively, named as
    a statement can use it. The second is the foreign keys among them that
    point *directly* into a candidate -- the only ones a ``DELETE`` of the
    candidates could break, since a table referencing a candidate only through
    another table is reached only through rows of that table.
    """
    cursor.execute(
        _REFERENCING_CLOSURE,
        {"candidates": [connection.ops.quote_name(name) for name in candidates]},
    )
    keys = cursor.fetchall()
    # Once per table, in the query's order, though a table holding several keys
    # into the closure comes back once per key.
    closure = list(dict.fromkeys(referencing for referencing, *_ in keys))
    references = [
        _Reference(referencing, referencing, tuple(columns), referenced)
        for referencing, referenced, into_candidate, columns in keys
        if into_candidate
    ]
    return closure, references


def _referencing_tables(connection: Any, cursor: Any, candidates: list[str]) -> list[_Reference]:
    """Every foreign key into a candidate, off PostgreSQL, through introspection.

    ``get_relations`` reads one table's foreign keys and is backend-neutral,
    so this asks every table in the database which of its columns point into a
    candidate. A candidate's own keys are skipped for the reason the PostgreSQL
    query skips them.
    """
    introspection = connection.introspection
    references: list[_Reference] = []
    for name in introspection.table_names(cursor):
        if name in candidates:
            continue
        for column, relation in introspection.get_relations(cursor, name).items():
            # By position, not by unpacking: the Django that added database
            # ON DELETE returns it as a third element, older ones return two.
            if relation[1] in candidates:
                references.append(
                    _Reference(connection.ops.quote_name(name), name, (column,), relation[1])
                )
    return references


def _refuse_held_references(connection: Any, cursor: Any, references: list[_Reference]) -> None:
    """Raise :class:`ShapeReferenced` if any row holds one of ``references``.

    A row references something only when every column of the key is set --
    PostgreSQL's default ``MATCH SIMPLE`` checks nothing for a row with any of
    them null, and Django's keys are single columns, where the two readings
    agree. Every reference that holds a row is named, not the first: a test
    setup that made one undeclared child usually made several.
    """
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
    declared = " and ".join(dict.fromkeys(reference.declared for reference in found))
    raise ShapeReferenced(
        f"A scaled world cannot empty {declared} without changing a table its shape does not "
        f"declare: rows it did not make reference the rows it would remove ({named}). A world "
        "removes the rows of the tables it declares and nothing else, so it refuses rather than "
        "leave those references pointing at nothing or take their rows along. "
        f"Declare {referencing} in the shape too, so those rows are the world's; give {declared} "
        "Disjoint keys (UuidKeys or Md5Keys), so the world builds beside the rows already there "
        "instead of emptying the table; or do not create those rows in this test."
    )


def _reset_declared_sequences(shape: Shape, using: str) -> None:
    """Point every declared table's sequence at the rows it now holds.

    A :class:`~django_data_shape.declaration.projection.Projection` is included: its rows
    come from a statement rather than from generated keys, but it still has a
    key column with a sequence behind it, and the rows it just lost were as real
    as any other's.
    """
    connection = connections[using]
    for table in shape.tables:
        reset_sequence(connection, table.model)
