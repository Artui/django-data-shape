"""One world at one scale factor, undone when the block ends."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from django.db import DEFAULT_DB_ALIAS, connections, transaction

from django_data_shape.build import build
from django_data_shape.keys.disjoint import Disjoint
from django_data_shape.scaled_shape import scaled_shape
from django_data_shape.shape import Shape
from django_data_shape.utils import reset_sequence


@contextmanager
def scaled_world(shape: Shape, factor: int, *, using: str = DEFAULT_DB_ALIAS) -> Iterator[int]:
    """Build ``shape`` at ``factor``, run the caller's block, then undo it.

    The implementation of :class:`~django_data_shape.scale_protocol.ScaleProtocol`
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
    sixteen statements for a two-table shape at every factor, because ``COPY``
    does not go through Django's ``execute_wrapper`` and only the emptiness
    check, the statistics-target read, the parent key read, the sequence reset,
    the ``ANALYZE`` and the savepoints do.

    That fourteen is counted with ``CaptureQueriesContext`` -- what
    ``django_assert_num_queries`` reads -- inside a non-transactional ``django_db``
    test. **Both halves of that sentence move the number**: the same shape counted
    through ``execute_wrapper``, which is what a capture built on that hook sees,
    is eleven, because the savepoints and the emptiness check reach the query log
    by a route the wrapper does not; and a ``transaction=True`` test drops one
    more savepoint. So do not read the absolute figure as a constant of this package.
    **What is invariant, and what the tests below pin, is the shape of each: fixed
    on PostgreSQL whatever the factor, growing off it.** Off PostgreSQL it is neither: the inserts are ordinary
    statements, one per thousand rows, **so the captured count grows with the
    factor** -- and a growth assertion measuring from outside the block would
    read the loader's own curve as its subject's.

    Both halves of that are pinned by tests rather than left as prose, because a
    measurement in a docstring is the first thing to rot and the consumer this
    matters to cannot check it without taking the dependency the protocol exists
    to avoid. The number above was already wrong once, for exactly that reason.

    **The teardown is a rollback, not a delete.** Building inside a transaction
    and rolling it back at the end restores exactly the state the block started
    from, which matters twice: this package never issues a destructive statement
    against a table it did not fill, and inside a pytest-django ``db`` test the
    rollback is to a savepoint, so it costs nothing and leaves the enclosing
    test transaction usable afterwards. Outside one it is an ordinary
    transaction rollback, so the same code is correct in both places.

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


def _empty_declared_tables(shape: Shape, using: str) -> None:
    """Clear the tables ``shape`` declares, inside the caller's transaction.

    A world at a factor is the declared shape at that size and nothing else, so
    rows already in those tables are not this world's -- and emptying them is
    what lets a scaled world be built over a session world, which is the one
    composition of this package's two pytest surfaces that an application with a
    single model graph needs and could not have.

    **Nothing is snapshotted, and nothing needs to be.** This runs inside the
    atomic block ``scaled_world`` already rolls back, so whatever was there
    comes back when the block ends -- the session world included. That is the
    whole reason the fix is four lines rather than a save-and-restore: the
    database was already going to undo this.

    Emptying rather than refusing is safe *only* here, and that is why it lives
    in this function and not in ``build``. A bare ``build()`` keeps its refusal,
    because it has no transaction of its own to undo and would be destroying
    rows for good.

    A table whose keys are :class:`~django_data_shape.keys.disjoint.Disjoint`
    is left alone, mirroring the exemption ``build`` makes for the same reason:
    those keys cannot collide with a caller's rows, so the hybrid this package
    documents -- parents made by your code, children made here -- must keep
    working. Deleting the parents would break it in a new way.
    """
    connection = connections[using]
    tables = [
        table.db_table
        for table in shape.tables
        if not isinstance(getattr(table, "keys", None), Disjoint)
    ]
    if not tables:
        return
    quoted = ", ".join(connection.ops.quote_name(name) for name in tables)
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            # TRUNCATE is transactional on PostgreSQL, so it rolls back with the
            # rest of the block. CASCADE covers a foreign key from a table this
            # shape does not declare; RESTART IDENTITY is deliberately omitted,
            # because a sequence reset is not transactional and would leave the
            # counter behind the rows that came back.
            cursor.execute(f"TRUNCATE {quoted} CASCADE")
        else:
            # DELETE rather than TRUNCATE off PostgreSQL: SQLite has no
            # TRUNCATE, and the loads here are small enough that the difference
            # does not matter.
            for name in reversed(tables):
                cursor.execute(f"DELETE FROM {connection.ops.quote_name(name)}")


def _reset_declared_sequences(shape: Shape, using: str) -> None:
    """Point every declared table's sequence at the rows it now holds.

    A :class:`~django_data_shape.projection.Projection` is included: its rows
    come from a statement rather than from generated keys, but it still has a
    key column with a sequence behind it, and the rows it just lost were as real
    as any other's.
    """
    connection = connections[using]
    for table in shape.tables:
        reset_sequence(connection, table.model)
