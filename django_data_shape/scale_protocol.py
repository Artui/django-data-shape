"""What a growth assertion asks a world for."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol


class ScaleProtocol(Protocol):
    """Make the world be at ``factor``, then let the caller run its block.

    The seam between this package and a consumer that asserts a query count is
    ``O(1)`` rather than ``O(N)`` by running one block at several scale factors.
    Such a consumer depends on **this shape**, not on this package: anything
    callable as ``at(factor)`` returning a context manager will do, so a project
    on a backend this package refuses -- or one that has not adopted it yet --
    supplies its own five-line callable and the assertion works unchanged.
    :func:`~django_data_shape.fixtures.scale_fixture.scale_fixture` yields an
    implementation of it, and
    :func:`~django_data_shape.scaled_world.scaled_world` bound to a shape is
    another.

    Two details are deliberate.

    **The factor is positional-only, and that is not a detail.** A structural
    type matches parameter names as well as types unless a parameter is marked
    positional-only, so without the ``/`` the protocol would have accepted only
    implementations that happened to spell the argument ``factor`` -- a protocol
    about this package's naming rather than about the shape of the call, and one
    that rejected the five-line callable the paragraph above offers. Callers pass
    the factor positionally; implementations name it whatever reads best.

    **It is a context manager, not a plain call.** A world has to be taken down
    as well as put up, and only whatever built it knows how to undo it. A
    protocol that only built would leave every implementation to invent its own
    teardown, and the one this package uses -- roll back to the savepoint the
    world was built inside -- is not something a caller could arrange from
    outside.

    **What it yields is a row count or nothing, never one of this package's
    types.** The value is how many rows the world actually holds, and it is a
    diagnostic rather than the growth curve's x-axis: the caller passed the
    factor in and already knows it. Yielding a ``BuildResult`` would have been
    richer and would have made the protocol unimplementable by anyone who has
    not installed this package, which is the opposite of what a seam is for.

    ``None`` is allowed for the same reason the value is optional in spirit
    already. The five-line callable the paragraph above offers is the one a
    consumer writes first::

        @contextmanager
        def world(n: int) -> Iterator[None]:
            build_my_fixtures(100 * n)
            yield

    and it has no count to report, only rows. Requiring one would have made the
    invitation false -- which it was, in this exact way, until the type below
    was widened. **A caller reading the value has to tolerate ``None``**; an
    implementation that can count cheaply should still yield the number, because
    a growth curve annotated with what the world actually held is worth more
    than one annotated with what was asked for.

    Worth recording, because this is the *second* time the docstring promised
    more than the signature allowed: the first was the parameter name, fixed by
    making ``factor`` positional-only, and both were found by a consumer rather
    than by review. A type-level promise with no type-level test behind it is
    what let each of them ship, so
    ``tests/scale_protocol_consumers.py`` now carries the invited implementation
    itself and the suite type-checks it.

    Spelled without this class -- for a consumer who would rather restate the
    shape than import it -- it is exactly::

        Callable[[int], AbstractContextManager[int | None]]

    Given here so that restatements converge on one, rather than on a looser
    ``ContextManager[Any]`` that would accept things this does not.
    """

    def __call__(self, factor: int, /) -> AbstractContextManager[int | None]: ...
