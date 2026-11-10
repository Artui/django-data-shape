from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TableResult:
    """Rows loaded into one table.

    Carries the database table name rather than the model, since that is what callers print or assert on."""

    table: str
    rows: int
