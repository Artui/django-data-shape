"""What a completed build reports back."""

from __future__ import annotations

from dataclasses import dataclass

from django_data_shape.table_result import TableResult


@dataclass(frozen=True)
class BuildResult:
    """The outcome of building a shape, table by table.

    Returned rather than logged because the counts are worth asserting on: the
    row count a shape declares and the row count a table holds are the same
    number today, but they stop being the same as soon as deduplication enters
    the picture with many-to-many edges. Reporting achieved counts from the
    start means that release changes what this says, not what callers have to
    start checking.
    """

    tables: tuple[TableResult, ...]

    @property
    def rows(self) -> int:
        """Rows loaded across every table."""
        return sum(result.rows for result in self.tables)
