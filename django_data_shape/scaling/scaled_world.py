"""One world at one scale factor, undone when the block ends."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, NamedTuple, cast

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import Model

from django_data_shape.declaration.projection import Projection
from django_data_shape.declaration.shape import Shape
from django_data_shape.declaration.table import Table
from django_data_shape.keys.disjoint import Disjoint
from django_data_shape.keys.uuid_keys import UuidKeys
from django_data_shape.loading.build import build
from django_data_shape.scaling.scaled_shape import scaled_shape
from django_data_shape.scaling.shape_referenced import ShapeReferenced
from django_data_shape.utils import primary_key_field, reset_sequence


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
    :class:`~django_data_shape.keys.disjoint.Disjoint` is not emptied, and the
    world builds beside the rows already there -- unless it has a foreign key
    into a declared table that is being emptied, directly or through another
    such table, and holds rows. Then it is emptied too: its rows are declared
    rows, and they cannot outlive the parents they point at. Those foreign keys
    are read from the models, not the database, so a
    ``ForeignKey(db_constraint=False)`` pulls a table in although the refusal
    below does not see one -- and a row referencing that table is then
    refused, where the database's own keys would have left nothing to refuse.

    **So a session world under a scaled world over the same graph needs no
    arrangement unless a declared ``Disjoint`` table holding the session's
    rows is left alone** -- one pointing at nothing the world empties, such as
    a UUID-keyed root, or any table of a graph keyed by UUIDs throughout. The
    world builds beside those rows, and its keys are a digest of each row's
    position and the shape's seed, which scaling keeps: with the session's seed
    the world makes the session's keys and the build fails on the primary key
    with ``IntegrityError``; with another seed it builds, and a table fanned
    out over that one draws parents from the session's rows as well as the
    world's.

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
    ``declared`` is the candidate's ``db_table`` as declared, on every backend,
    which is what lets the refusal find the declaration it is about.
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
    working. Unless it points into what is emptied: see :func:`_candidates`.

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
    (``test_the_delete_route_empties_children_before_parents``), and on
    PostgreSQL the pending checks are fired after them
    (``test_a_violation_outside_the_declaration_surfaces_at_entry_on_either_route``).

    **Off PostgreSQL the refusal is made through Django's introspection**,
    which reads every table's foreign keys -- a cost paid only by a world whose
    declared tables already hold rows, since one over empty tables stops at the
    first read. It is the same rule, so a world's ``DELETE`` does not reach an
    undeclared table through a database-level ``ON DELETE``, or leave its rows
    pointing at keys the world then hands to rows of its own. The reading
    differs in one place: introspection reports each column of a composite
    foreign key on its own, so a composite key counts there as soon as any
    column is set (``test_off_postgresql_a_composite_key_counts_when_any_column_is_set``).
    Django never creates one.
    """
    connection = connections[using]
    if all(_keeps_its_keys(table) for table in shape.tables):
        # Nothing here is ever emptied on its own account, so there is nothing
        # to read either: a shape declaring only Disjoint tables issues no
        # statement at all
        # (test_a_shape_whose_tables_all_keep_their_own_keys_empties_nothing).
        return
    with connection.cursor() as cursor:
        # Every declared table in the one read, Disjoint ones included, because
        # whether a Disjoint table holds rows decides whether it joins the
        # candidates below -- and asking in the same statement costs nothing.
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
                # ends with SET CONSTRAINTS ALL DEFERRED, which defers every
                # deferrable constraint, one declared INITIALLY IMMEDIATE
                # included, for the rest of the block and the caller's block
                # with it (test_inside_the_block_every_deferrable_constraint_is_deferred,
                # on both routes). It does not outlive the block, because the
                # mode is transaction state and rolling the block back restores
                # it with everything else
                # (test_the_constraint_mode_the_world_set_does_not_outlive_it).
                # Before the statement on this route, because TRUNCATE is what
                # refuses; the DELETE route below fires them after its
                # statements instead, for the build's ALTER TABLE.
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
        _refuse_held_references(connection, cursor, references, shape, candidates)
        # DELETE, because TRUNCATE cannot remove the declared rows here without
        # taking a table that references them along: its unit is a set closed
        # under references, and one of those tables holds rows that are not
        # this world's. The refusal above has just established that none of
        # those rows references a candidate, so removing every candidate row
        # leaves them untouched and nothing dangling. Children before parents,
        # in the declaration's own order reversed.
        for name in reversed(candidates):
            cursor.execute(f"DELETE FROM {connection.ops.quote_name(name)}")
        if connection.vendor == "postgresql":
            # Fired after the DELETEs, where the TRUNCATE route fires them
            # before its statement. DELETE itself is allowed with trigger events
            # pending, so nothing here needs them first; they are fired so that
            # both routes enter the block alike. A row outside the declaration
            # that breaks a constraint raises here, under the constraint's
            # name, as it does before a TRUNCATE
            # (test_a_violation_outside_the_declaration_surfaces_at_entry_on_either_route),
            # and every deferrable constraint is deferred for the rest of the
            # block, which the rollback undoes
            # (test_inside_the_block_every_deferrable_constraint_is_deferred).
            #
            # Every check the DELETEs queued passes -- each row they remove
            # from a referenced table queues one: the refusal above has ruled
            # out a reference from outside the declaration, and the children
            # went first. One the caller queued on a row of a declared table is
            # skipped, since the row is gone -- so an orphan there, which the
            # TRUNCATE route raises for, is removed unchecked here
            # (test_on_the_delete_route_an_orphan_in_a_declared_table_is_removed_unchecked).
            # The build's ALTER TABLE ... SET STATISTICS, which PostgreSQL
            # refuses while checks are queued, does not rest on this call:
            # apply_statistics_targets fires them itself before any ALTER,
            # because a world that empties nothing reaches it with the
            # caller's checks still queued.
            #
            # Off PostgreSQL nothing refuses a pending check, and SQLite's
            # version of this call scans every table in the database.
            connection.check_constraints()


def _keeps_its_keys(table: Table | Projection) -> bool:
    """Whether ``table`` builds beside rows already there rather than being emptied.

    A :class:`~django_data_shape.declaration.projection.Projection` has no
    ``keys`` to ask, and its rows come from a statement, so it never does.
    """
    return isinstance(getattr(table, "keys", None), Disjoint)


def _candidates(shape: Shape, holding: set[str]) -> list[str]:
    """The declared tables to empty, in declaration order, given which hold rows.

    Every declared table holding rows whose keys are not
    :class:`~django_data_shape.keys.disjoint.Disjoint` -- and then every
    Disjoint one holding rows that has a foreign key into that set, repeated
    until nothing more joins. A Disjoint table is exempt from emptying because
    its keys cannot collide with rows already there, which is a reason to
    leave its rows beside the world's and no reason to leave them pointing at
    parents the world removes. Its rows are declared rows, so removing them
    stays inside the rule this function keeps; leaving them would make the
    world refuse its own declaration, naming a table the shape declares as if
    it were somebody else's.

    Decided by schema and not row by row, like the ``TRUNCATE`` closure: a
    Disjoint table joins when one of its model's foreign keys points into the
    set, whatever its rows hold there. Read from the models rather than the
    database, so it costs no statement -- and so a ``db_constraint=False`` key,
    which the refusal reads past because the database holds no constraint for
    it, still pulls its table in. A row referencing that table is then refused,
    where by the database's keys alone there was nothing of the table's to
    empty
    (``test_a_key_the_database_does_not_enforce_still_joins_a_disjoint_table``).

    Each condition fails a test of its own when it is removed, which a branch
    gate cannot show. Joining every Disjoint table, rather than one with a key
    into the set, fails
    ``test_a_disjoint_parent_the_world_does_not_reach_keeps_its_rows``. Joining
    one that holds no rows fails
    ``test_an_empty_disjoint_table_carries_nothing_into_the_set``, where a
    table holding rows would follow it in. Stopping after one round, so that
    only keys into the first set count, fails
    ``test_a_disjoint_table_reached_through_another_joins_too``. Without the
    set difference the loop never ends, and
    ``test_a_session_world_with_a_disjoint_child_sits_under_the_same_graph``
    never returns.
    """
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
    """Whether each ``(table, condition)`` matches a row, in one statement.

    One statement however many tables there are, so what a world costs stays
    a property of the world rather than of the schema. No statement at all for
    no probes, which is how a candidate nothing references costs one read
    fewer: its closure is empty, and so is the question of whether it holds
    rows (``test_and_one_fewer_where_nothing_references_the_declared_tables``).
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
        _Reference(referencing, referencing, tuple(columns), candidates[position - 1])
        for referencing, position, columns in keys
        if position is not None
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


def _refuse_held_references(
    connection: Any, cursor: Any, references: list[_Reference], shape: Shape, candidates: list[str]
) -> None:
    """Raise :class:`ShapeReferenced` if any row holds one of ``references``.

    A row references something only when every column of the key is set --
    PostgreSQL's default ``MATCH SIMPLE`` checks nothing for a row with any of
    them null, and Django's keys are single columns, where the two readings
    agree. Every reference that holds a row is named, not the first: a test
    setup that made one undeclared child usually made several.

    The ``Disjoint`` way out is offered only where taking it would work for
    every declared table the message names, since each way out is offered as
    one that ends the refusal on its own: where the primary key takes the keys
    (see :func:`_disjoint_keys_fit`), and where the table has no foreign key
    into another of ``candidates``, through which :func:`_candidates` would
    pull it straight back into the set.
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
    names = list(dict.fromkeys(reference.declared for reference in found))
    declared = " and ".join(names)
    by_name = {table.db_table: table for table in shape.tables}
    # Every named table, not any: following the advice for one of two leaves
    # the other's reference refused, so the advice would not be a way out
    # (test_disjoint_keys_are_not_offered_unless_every_named_table_can_take_them).
    # And none with a key into another candidate, because _candidates pulls a
    # Disjoint table back into the set through exactly such a key, and the
    # refusal comes back unchanged
    # (test_disjoint_keys_are_not_offered_where_the_table_would_be_emptied_all_the_same).
    # A key into itself does not count: a table the advice takes out of the
    # set cannot pull itself back in
    # (test_a_key_into_itself_does_not_withhold_disjoint_keys).
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
    """Whether giving ``table`` Disjoint keys would take it out of the emptying.

    Not for a :class:`~django_data_shape.declaration.projection.Projection`,
    whose keys a scaled world never reads, so it is emptied whatever they are
    (``test_disjoint_keys_are_not_offered_for_a_projected_table``); and not for
    a table whose keys already are Disjoint, which is emptied only because it
    references another candidate
    (``test_disjoint_keys_are_not_offered_for_a_table_that_has_them``). Both
    disjuncts were removed in turn and the test named beside each failed.

    Otherwise only where the primary key accepts the keys those strategies
    make, as the field's own validation decides. A ``UUIDField`` does, and so
    does a text column that can hold a UUID's 36 characters
    (``test_disjoint_keys_are_offered_for_a_character_key_that_holds_them``);
    an integer key never does, and following the advice there used to fail at
    the load, out of range for ``bigint`` on PostgreSQL and too large for an
    ``INTEGER`` on SQLite (``test_a_fee_the_caller_made_refuses_the_world_naming_its_column``).

    Two things are decided before the field is asked, and each was removed in
    turn to watch the test named beside it fail. A primary key that is itself a
    foreign key -- a one-to-one key into its parent -- never accepts them, and
    is not asked, because asking runs ``ForeignKey.validate``'s existence query
    on the router's database rather than the world's
    (``test_disjoint_keys_are_not_offered_for_a_key_that_is_a_relation``, which
    asserts that nothing reads the parent). And a field that refuses with
    ``ValueError`` or ``TypeError``, where Django's contract is a
    ``ValidationError``, is taken to refuse, rather than raising in place of
    the refusal; each of the two is exercised on its own
    (``test_a_key_field_refusing_outside_its_contract_still_refuses_the_world``),
    since one ``except`` naming both is a single branch to the coverage gate.
    """
    keys = getattr(table, "keys", None)
    if keys is None or isinstance(keys, Disjoint):
        return False
    field = primary_key_field(table.model)
    if field.is_relation:
        # A primary key that is a foreign key holds its parent's keys, and a
        # digest is never one of them. Asked anyway, the field would answer
        # with ForeignKey.validate's existence query, run on the router's
        # database whatever the world's is.
        return False
    try:
        field.clean(_DISJOINT_KEY, None)
    except (ValidationError, ValueError, TypeError):
        # ValidationError is the contract. The other two are what a field
        # breaking it raises from to_python, and either would otherwise escape
        # in place of the refusal this is building.
        return False
    return True


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
