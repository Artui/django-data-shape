from __future__ import annotations

from typing import Protocol


class KeyStrategy(Protocol):
    """Turns a row index into that row's primary key.

    The generalisation of what used to be a hard-coded dense ``1..N`` range."""

    def key_for(self, row: int, stream: int) -> object: ...
