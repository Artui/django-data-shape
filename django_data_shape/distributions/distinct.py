from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Distinct(Protocol):
    """A distribution whose value differs in every row of the table.

    The sixth opt-in protocol, and the exact dual of ``Bounded``."""

    def is_distinct_per_row(self) -> bool: ...
