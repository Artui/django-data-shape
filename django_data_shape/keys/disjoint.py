from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Disjoint(Protocol):
    """A key strategy that cannot produce a key some existing row already holds.

    The opt-in protocol behind one refusal: building into a table that already has rows."""

    def is_disjoint_from_existing_rows(self) -> bool: ...
