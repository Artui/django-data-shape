"""Heavy-tailed weights: a few very large, a long tail of very small."""

from __future__ import annotations

import math

from django_data_shape.invalid_shape import InvalidShape


class Zipf:
    """Positive weights following a power law of exponent ``s``.

    The distribution fan-out is realistically drawn from, and the reason
    declaring fan-out is worth doing at all. A customer table where every
    customer has ten orders is not merely tidy, it is the one shape in which the
    planner is never wrong: its ``n_distinct`` average *is* the truth, so a join
    estimate cannot miss. Give the head a thousand orders and the tail one, and
    the same estimate is out by orders of magnitude in both directions -- which
    is what production looks like and what a test database has to reproduce.

    Inverse transform of a Pareto: ``(1 - draw) ** (-1 / s)``. Larger ``s``
    means a lighter tail; values near 1 are the classic Zipf regime.
    """

    def __init__(self, s: float = 1.2) -> None:
        if not math.isfinite(s) or s <= 0:
            raise InvalidShape(f"Zipf needs a positive finite exponent, got s={s}.")
        self._s = s

    def value(self, row: int, draw: float) -> object:
        # ``1 - draw`` rather than ``draw`` so the singularity sits at the top of
        # the interval: draw is in [0, 1), so 1 - draw is in (0, 1] and never
        # zero, which keeps the power finite for every possible draw.
        return (1.0 - draw) ** (-1.0 / self._s)

    def __repr__(self) -> str:
        return f"Zipf({self._s!r})"
