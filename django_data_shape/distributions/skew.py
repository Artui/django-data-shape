from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from django_data_shape.declaration.invalid_shape import InvalidShape


class Skew:
    """Values drawn from a weighted set, in a fixed order.

    The distribution the package exists for: a 98% status column decides whether an index helps."""

    def __init__(self, weights: Mapping[Any, float]) -> None:
        if not weights:
            raise InvalidShape("Skew needs at least one value; an empty distribution has none.")
        # not (w > 0) rather than w <= 0: a NaN weight compares False to both and slips through.
        bad = sorted(repr(v) for v, w in weights.items() if not (w > 0) or not math.isfinite(w))
        if bad:
            raise InvalidShape(
                "Skew weights must be positive and finite, and these are not: "
                + ", ".join(bad)
                + ". A value that never occurs is said by omitting it."
            )
        total = sum(weights.values())
        self._values: list[object] = []
        self._bounds: list[float] = []
        running = 0.0
        for value, weight in weights.items():
            running += weight
            self._values.append(value)
            self._bounds.append(running / total)
        self._weights = dict(weights)

    def value(self, row: int, draw: float) -> object:
        for value, bound in zip(self._values, self._bounds, strict=True):
            if draw < bound:
                return value
        # Rounding can leave the last bound just under 1.0, so the fallthrough is the last value.
        return self._values[-1]

    def distinct_values(self) -> int:
        """How many values were declared. See ``Bounded``."""
        return len(self._values)

    def shares(self) -> dict[object, float]:
        """Each declared value against its share of the rows. See ``Categorical``."""
        total = sum(self._weights.values())
        return {value: weight / total for value, weight in self._weights.items()}

    def canonical(self) -> object:
        """The weights, **in declaration order**, which decides the values. See ``Canonical``."""
        return (self._weights,)

    def __repr__(self) -> str:
        return f"Skew({self._weights!r})"
