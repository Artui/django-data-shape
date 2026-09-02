"""One world at one scale factor, undone when the block ends."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from django.db import DEFAULT_DB_ALIAS, transaction

from django_data_shape.build import build
from django_data_shape.scaled_shape import scaled_shape
from django_data_shape.shape import Shape


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
    fourteen statements for a two-table shape at every factor, because ``COPY``
    does not go through Django's ``execute_wrapper`` and only the emptiness
    check, the parent key read, the sequence reset, the ``ANALYZE`` and the
    savepoints do.

    That fourteen is counted with ``CaptureQueriesContext`` -- what
    ``django_assert_num_queries`` reads -- inside a non-transactional ``django_db``
    test. **Both halves of that sentence move the number**: the same shape counted
    through ``execute_wrapper``, which is what a capture built on that hook sees,
    is nine, because the savepoints and the emptiness check reach the query log by
    a route the wrapper does not; and a ``transaction=True`` test drops one more
    savepoint. So do not read the absolute figure as a constant of this package.
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
    with transaction.atomic(using=using):
        result = build(scaled_shape(shape, factor), using=using, require_statistics=False)
        yield result.rows
        # Rolling back on the way out rather than raising and swallowing an
        # exception to get there: set_rollback is the supported way to leave an
        # atomic block without keeping it. An exception from the caller's block
        # never reaches this line and does not need to -- atomic rolls back for
        # exactly that case already.
        transaction.set_rollback(True, using=using)
