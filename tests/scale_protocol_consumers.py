"""Implementations of the scale protocol that a consumer might actually write.

Not collected by pytest: this file exists to be **type-checked**, by
``tests/test_scale_protocol.py``, which runs ``ty`` over it. The protocol's whole
promise is that a package which has never installed this one can satisfy it with
a five-line callable, and that promise is a type-level claim -- so the only test
that can hold it is a type-level one. Without this the protocol shipped rejecting
the very implementations its docstring offered, because ``ty`` matches a
parameter's *name* unless it is positional-only.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from functools import partial

from django_data_shape import Constant, ScaleProtocol, Shape, Table, scaled_world
from tests.testapp.models import Company


# The one from the documentation: a consumer on a backend this package refuses,
# who builds their world with the ORM. The parameter is deliberately not called
# ``factor`` -- a protocol that only accepted that spelling would be a protocol
# about our naming rather than about the shape of the call.
@contextmanager
def hand_rolled(n: int) -> Iterator[int]:
    made = [Company(name="acme") for _ in range(100 * n)]
    try:
        yield len(made)
    finally:
        made.clear()


# The same thing with state, because a consumer wanting to count builds or hold a
# connection reaches for a class and would otherwise find the protocol closed.
class Recorded:
    def __init__(self) -> None:
        self.factors: list[int] = []

    def __call__(self, size: int, /) -> AbstractContextManager[int]:
        self.factors.append(size)
        return hand_rolled(size)


_SHAPE = Shape(Table(Company, rows=100, name=Constant("acme")))

# And this package's own, which has to satisfy its own protocol or the seam is
# one nobody stands on either side of.
_bound = partial(scaled_world, _SHAPE)

hand_rolled_is_one: ScaleProtocol = hand_rolled
recorded_is_one: ScaleProtocol = Recorded()
bound_scaled_world_is_one: ScaleProtocol = _bound
