from __future__ import annotations

from typing import cast

from django_data_shape.derivations.scope import Scope
from django_data_shape.distributions.distribution import Distribution

# The largest double below 1.0. Reversing a rank as ``1 - draw`` is correct
# except at the one point it is not: a draw of exactly 0.0 reverses to exactly
# 1.0, which is outside the [0, 1) interval every distribution is written
# against -- and Zipf, which is what a rank like this usually feeds, raises
# ZeroDivisionError there rather than returning something wrong. Clamping costs
# one comparison per row and removes the case entirely.
_BELOW_ONE = 1.0 - 2.0**-53


class Aligned:
    """``distribution`` read at a rank shared with every column naming it.

    Independent marginals produce a database that is realistic per column and unrealistic per **entity**."""

    def __init__(self, rank: str, distribution: Distribution, *, reverse: bool = False) -> None:
        self._sources = (rank,)
        self._distribution = distribution
        self._reverse = reverse

    @property
    def scope(self) -> Scope:
        return Scope.RANK

    @property
    def sources(self) -> tuple[str, ...]:
        return self._sources

    def value(self, row: int, draw: float, sources: tuple[object, ...]) -> object:
        # The shared rank replaces this column's own draw, which is what makes the alignment exact.
        rank = cast("float", sources[0])
        if self._reverse:
            rank = min(1.0 - rank, _BELOW_ONE)
        return self._distribution.value(row, rank)

    def canonical(self) -> object:
        """The rank, the distribution read at it, and the direction. See ``Canonical``."""
        return (self._sources, self._distribution, self._reverse)

    def __repr__(self) -> str:
        return f"Aligned({self._sources[0]!r}, {self._distribution!r}, reverse={self._reverse!r})"
