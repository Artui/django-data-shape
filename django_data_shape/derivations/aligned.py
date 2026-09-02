"""Columns that are extreme in the same rows."""

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

    Independent marginals produce a database that is realistic per column and
    unrealistic per **entity**: the biggest accounts are not the ones with the
    most tickets, the most storage or the longest history, because each of those
    was drawn on its own. No single row is extreme in two ways at once -- and
    that row is the one that breaks production, and the one a performance test
    is supposed to find.

    A rank is a name the declaration invents. Every column declaring the same
    rank reads the same draw, so their orderings agree exactly:

    ```python
    Table(
        Account,
        rows=50_000,
        storage_bytes=Aligned("size", Uniform(1e6, 1e12)),
        seat_count=Aligned("size", Zipf(1.1)),
        trial_days_left=Aligned("size", Uniform(0, 30), reverse=True),
    )
    ```

    ``reverse=True`` reads the same rank from the other end, which is how a
    column that is *inversely* related to the others is said. The coupling is
    exact in both directions and has no strength parameter: a partial coupling
    is a copula, and a copula is a research project wearing a small API. Exact
    or reversed covers the shape this exists for, and a declaration that needs
    something in between is better served by
    :class:`~django_data_shape.derivations.derived.Derived` over a rank source,
    which can compute whatever it likes from the same draw.

    Ranks are **per table**. Two tables using the name ``"size"`` share nothing,
    because the only thing they could align on is the row index, and row 40 of
    one table has no relationship to row 40 of another.

    One thing this cannot do for you: a distribution that ignores its draw
    aligns to nothing. :class:`~django_data_shape.distributions.sequential.Sequential`
    is a function of the row index and
    :class:`~django_data_shape.distributions.constant.Constant` of neither, so
    wrapping either in an ``Aligned`` is accepted and does nothing. It is not
    refused because a distribution declares no such thing about itself, and
    guessing from the type would refuse a caller's own perfectly good one.
    """

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
        # The shared rank replaces this column's own draw rather than perturbing
        # it. That is what makes the alignment exact, and it is why the draw
        # argument is ignored here: a column's own stream is what independence
        # looks like, and independence is the thing being declared away.
        # cast, not a conversion: a rank source is resolved by this package and
        # is a draw in [0, 1) by construction, so a runtime check here would be
        # a branch no declaration can reach.
        rank = cast("float", sources[0])
        if self._reverse:
            rank = min(1.0 - rank, _BELOW_ONE)
        return self._distribution.value(row, rank)

    def canonical(self) -> object:
        """The rank, the distribution read at it, and the direction. See ``Canonical``."""
        return (self._sources, self._distribution, self._reverse)

    def __repr__(self) -> str:
        return f"Aligned({self._sources[0]!r}, {self._distribution!r}, reverse={self._reverse!r})"
