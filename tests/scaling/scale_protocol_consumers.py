from __future__ import annotations

from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from functools import partial

from django_data_shape import Constant, ScaleProtocol, Shape, Table, scaled_world
from tests.testapp.models import Company


# A consumer on a refused backend builds rows with the ORM, and names its parameter freely.
@contextmanager
def hand_rolled(n: int) -> Generator[int]:
    made = [Company(name="acme") for _ in range(100 * n)]
    try:
        yield len(made)
    finally:
        made.clear()


# It builds rows and yields nothing, because it has rows rather than a count.
@contextmanager
def bare_world(n: int) -> Generator[None]:
    made = [Company(name="acme") for _ in range(100 * n)]
    try:
        yield
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
bare_world_is_one: ScaleProtocol = bare_world
recorded_is_one: ScaleProtocol = Recorded()
bound_scaled_world_is_one: ScaleProtocol = _bound
