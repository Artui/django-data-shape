from __future__ import annotations

import math

from django_data_shape.declaration.invalid_shape import InvalidShape


class Zipf:
    """Positive weights following a power law of exponent ``s``.

    The distribution fan-out is realistically drawn from, and the reason fan-out is worth declaring."""

    def __init__(self, s: float = 1.2) -> None:
        if not math.isfinite(s) or s <= 0:
            raise InvalidShape(f"Zipf needs a positive finite exponent, got s={s}.")
        self._s = s

    def value(self, row: int, draw: float) -> object:
        # 1 - draw keeps the singularity at the top of the interval, so the power stays finite.
        return (1.0 - draw) ** (-1.0 / self._s)

    def canonical(self) -> object:
        """The exponent. See ``Canonical``."""
        return (self._s,)

    def __repr__(self) -> str:
        return f"Zipf({self._s!r})"
