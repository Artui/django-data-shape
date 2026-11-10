from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Bounded(Protocol):
    """A distribution with a known, finite number of distinct values.

    A second protocol rather than a method on ``Distribution``, so only bounded ones opt in."""

    def distinct_values(self) -> int: ...
