"""A column that advances with the row, instead of scattering."""

from __future__ import annotations

from typing import Any


class Sequential:
    """``start`` plus ``row`` steps: monotonic, and correlated with the key.

    The point is the correlation, not the convenience. Postgres records a
    correlation statistic per column and costs an index scan differently
    depending on it, so a timestamp column filled with shuffled dates plans
    differently from one that advances the way real rows arrive. Shuffling is
    the easy thing to do by accident and it is wrong in a way that only shows up
    in plan choice.

    Works for anything supporting ``start + row * step``, which covers numbers
    and ``datetime`` with a ``timedelta``. It ignores ``draw`` entirely: this is
    the one distribution whose value is a function of position alone.
    """

    def __init__(self, start: Any, step: Any) -> None:
        self._start = start
        self._step = step

    def value(self, row: int, draw: float) -> object:
        return self._start + row * self._step

    def canonical(self) -> object:
        """The start and the step. See ``Canonical``."""
        return (self._start, self._step)

    def __repr__(self) -> str:
        return f"Sequential({self._start!r}, {self._step!r})"
