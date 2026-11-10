from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TableResult:
    """Rows loaded into one table.

    Carries the database table name, not the model, since callers print or assert on that."""

    table: str
    rows: int
