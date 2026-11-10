from __future__ import annotations

from typing import Any


class Sequential:
    """``start`` plus ``row`` steps: monotonic, and correlated with the key.

    The point is the correlation, not the convenience: Postgres keeps a statistic for it per column."""

    def __init__(self, start: Any, step: Any) -> None:
        self._start = start
        self._step = step

    def value(self, row: int, draw: float) -> object:
        return self._start + row * self._step

    def is_distinct_per_row(self) -> bool:
        """Whether every row gets its own value. See ``Distinct``."""
        return bool(self._step != self._step * 0)

    def is_ascending(self) -> bool:
        """Whether the step moves values up rather than down. See ``Ascending``."""
        return bool(self._step > self._step * 0)

    def canonical(self) -> object:
        """The start and the step. See ``Canonical``."""
        return (self._start, self._step)

    def __repr__(self) -> str:
        return f"Sequential({self._start!r}, {self._step!r})"
