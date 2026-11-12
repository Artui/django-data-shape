from __future__ import annotations

import math
from decimal import Decimal, localcontext

from django_data_shape.declaration.invalid_shape import InvalidShape


class Uniform:
    """Values spread evenly between ``low`` and ``high``.

    Deliberately the least interesting distribution in the package, and named
    plainly so it reads as a choice. Most real columns are not uniform, and a
    uniform declaration on a column that matters is usually a placeholder
    somebody meant to come back to.

    ``places`` rounds the result. Not because the column would reject the
    unrounded value -- Postgres rounds a float to a ``numeric(10, 2)`` happily,
    and only overflowing the declared precision is an error -- but because a
    money column whose values carry full binary float noise is not what the
    application would ever have written, and this package's whole claim is that
    the loaded rows are ones it could have. Rounding to ``Decimal`` rather than
    ``float`` keeps the value exact on the way into ``COPY``; ``places=0`` is
    how a plain integer column is filled.
    """

    def __init__(self, low: float, high: float, places: int | None = None) -> None:
        # isfinite first: NaN compares False to every ordering, so it would pass ``high <= low``.
        if not math.isfinite(low) or not math.isfinite(high):
            raise InvalidShape(f"Uniform needs finite bounds, got low={low}, high={high}.")
        if high <= low:
            raise InvalidShape(f"Uniform needs high greater than low, got low={low}, high={high}.")
        if places is not None and places < 0:
            raise InvalidShape(f"Uniform places cannot be negative, got {places}.")
        self._low = low
        self._high = high
        self._places = places
        magnitude = max(abs(low), abs(high), 1.0)
        self._precision = max(28, int(math.log10(magnitude)) + (places or 0) + 5)

    def value(self, row: int, draw: float) -> object:
        raw = self._low + draw * (self._high - self._low)
        if self._places is None:
            return raw
        with localcontext() as context:
            context.prec = self._precision
            # repr(), not the float: Decimal(2.675) rounds to 2.67, where the literal means 2.68.
            return round(Decimal(repr(raw)), self._places)

    def canonical(self) -> object:
        """The bounds and the rounding. See ``Canonical``.

        The precision computed in ``__init__`` is left out, because it is a
        function of these three and adding it could only ever agree with them.
        """
        return (self._low, self._high, self._places)

    def __repr__(self) -> str:
        places = "" if self._places is None else f", places={self._places}"
        return f"Uniform({self._low!r}, {self._high!r}{places})"
