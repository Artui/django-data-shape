"""What one table's load actually produced."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TableResult:
    """Rows loaded into one table.

    Carries the database table name rather than the model, because this is what
    a caller prints or asserts on, and because the two stop being one-to-one as
    soon as through tables are generated alongside the models that declare them.
    """

    table: str
    rows: int
