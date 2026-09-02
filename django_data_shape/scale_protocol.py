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

    **What it yields is a number, not one of this package's types.** The value
    is how many rows the world actually holds, which is a diagnostic rather than
    the growth curve's x-axis: the caller passed the factor in and already knows
    it. Yielding a ``BuildResult`` would have been richer and would have made
    the protocol unimplementable by anyone who has not installed this package,
    which is the opposite of what a seam is for.
    """

    def __call__(self, factor: int, /) -> AbstractContextManager[int]: ...
