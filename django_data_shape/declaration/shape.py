from __future__ import annotations

from collections.abc import Sequence

from django_data_shape.declaration.check_constraints import check_constraints
from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.declaration.order_tables import order_tables
from django_data_shape.declaration.projection import Projection
from django_data_shape.declaration.table import Table
from django_data_shape.invariants.invariant import Invariant


class Shape:
    """The tables to build and the seed that makes them reproducible; holds no connection."""

    def __init__(
        self,
        *tables: Table | Projection,
        seed: int = 0,
        invariants: Sequence[Invariant] = (),
    ) -> None:
        if not tables:
            raise InvalidShape("A shape needs at least one table.")
        seen: dict[str, Table | Projection] = {}
        for table in tables:
            key = table.db_table
            if key in seen:
                raise InvalidShape(
                    f"{key} is declared twice. One table gets one row count and one set of "
                    "distributions; two declarations would silently mean whichever came last."
                )
            seen[key] = table
        check_constraints(tables)
        # The plan is discarded: it exists only for the refusal, and a shape stays inert data.
        order_tables(tables)
        self._tables = tables
        self._seed = seed
        self._invariants = tuple(invariants)

    # Read-only, because a shape has to stay inert data that a checked declaration cannot change.
    @property
    def tables(self) -> tuple[Table | Projection, ...]:
        return self._tables

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def invariants(self) -> tuple[Invariant, ...]:
        """The rules the loaded data is checked against, after every table is in."""
        return self._invariants

    def canonical(self) -> object:
        """The seed and every declaration, keyed by table. See ``Canonical``."""
        return (
            self.seed,
            {table.db_table: table for table in self.tables},
            self._invariants,
        )

    def __repr__(self) -> str:
        names = ", ".join(table.model.__name__ for table in self.tables)
        return f"Shape({names}, seed={self.seed})"
