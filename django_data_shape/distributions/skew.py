"""A categorical column with a declared imbalance."""

from __future__ import annotations

from django_data_shape.invalid_shape import InvalidShape


class Skew:
    """Values drawn from a weighted set, in a fixed order.

    This is the distribution the package exists for. A status column that is 98%
    one value is what decides whether an index on it is usable at all, and it is
    the thing a fixtures loop never expresses -- ten rows with one of each says
    the opposite of what production says.

    Weights are relative and need not sum to 1: the readable form is often
    counts, and normalising here is cheaper than making every caller do it. They
    must be positive, because a zero-weight value is a value that never appears,
    which is better said by leaving it out than by declaring it and meaning not.
    """

    def __init__(self, weights: dict[object, float]) -> None:
        if not weights:
            raise InvalidShape("Skew needs at least one value; an empty distribution has none.")
        bad = sorted(repr(v) for v, w in weights.items() if w <= 0)
        if bad:
            raise InvalidShape(
                "Skew weights must be positive, and these are not: "
                + ", ".join(bad)
                + ". A value that never occurs is said by omitting it."
            )
        total = sum(weights.values())
        # The cumulative bounds are precomputed once rather than per row: this
        # runs a million times or more, and the alternative is re-summing the
        # weights inside the hot loop.
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
        # Only reachable when floating-point accumulation leaves the final bound
        # a hair below 1.0. The last value is the correct answer there, and
        # falling through to it is cheaper than renormalising every draw.
        return self._values[-1]

    def __repr__(self) -> str:
        return f"Skew({self._weights!r})"
