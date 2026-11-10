from __future__ import annotations

from dataclasses import dataclass

from django_data_shape.types.table_result import TableResult


@dataclass(frozen=True)
class BuildResult:
    """The outcome of building a shape, table by table.

    Returned rather than logged because the counts are worth asserting on."""

    tables: tuple[TableResult, ...]

    @property
    def rows(self) -> int:
        """Rows loaded across every table."""
        return sum(result.rows for result in self.tables)
