"""The same value in every row."""

from __future__ import annotations


class Constant:
    """Every row gets ``value``.

    Present because a column that never varies is a real shape, not a missing
    declaration: a tenant id on a single-tenant fixture, or a flag that is false
    for every row in the dataset under test. Declaring it says so, where leaving
    it out would mean the field simply had no distribution.

    It also has a planner consequence worth knowing: a single-valued column has
    exactly one most-common value at frequency 1.0, so a filter on it is either
    everything or nothing, and an index on it is never usable.
    """

    def __init__(self, value: object) -> None:
        self._value = value

    def value(self, row: int, draw: float) -> object:
        return self._value

    def distinct_values(self) -> int:
        """One, by definition. See ``Bounded``."""
        return 1

    def canonical(self) -> object:
        """The one value, which is the whole declaration. See ``Canonical``."""
        return (self._value,)

    def __repr__(self) -> str:
        return f"Constant({self._value!r})"
