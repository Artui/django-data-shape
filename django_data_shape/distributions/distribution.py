from __future__ import annotations

from typing import Protocol


class Distribution(Protocol):
    """Produces the value of one field for one row.

    Both arguments reach every implementation, since each kind needs a different half."""

    def value(self, row: int, draw: float) -> object: ...
