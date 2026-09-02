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
        result = build(scaled_shape(shape, factor), using=using)
        yield result.rows
        # Rolling back on the way out rather than raising and swallowing an
        # exception to get there: set_rollback is the supported way to leave an
        # atomic block without keeping it. An exception from the caller's block
        # never reaches this line and does not need to -- atomic rolls back for
        # exactly that case already.
        transaction.set_rollback(True, using=using)
