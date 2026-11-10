from __future__ import annotations


class Constant:
    """Every row gets ``value``.

    Present because a column that never varies is a real shape, not a missing declaration."""

    def __init__(self, value: object) -> None:
        self._value = value

    def value(self, row: int, draw: float) -> object:
        return self._value

    def distinct_values(self) -> int:
        """One, by definition. See ``Bounded``."""
        return 1

    def shares(self) -> dict[object, float]:
        """The one value, holding all of the rows. See ``Categorical``."""
        return {self._value: 1.0}

    def canonical(self) -> object:
        """The one value, which is the whole declaration. See ``Canonical``."""
        return (self._value,)

    def __repr__(self) -> str:
        return f"Constant({self._value!r})"
