from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SqlKeys(Protocol):
    """A ``KeyStrategy`` with a SQL twin, so a projection can compute the keys itself.

    Every table has a declared row count, so keys are enumerated in Python for ``COPY``."""

    def key_sql(self, stream: int, row: str) -> str: ...
