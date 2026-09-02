"""A whole declared database."""

from __future__ import annotations

from django_data_shape.invalid_shape import InvalidShape
from django_data_shape.projection import Projection
from django_data_shape.table import Table


class Shape:
    """The tables to build, and the seed that makes them reproducible.

    A declaration and nothing else: it holds no connection, opens nothing, and
    has no ``build`` method. Building lives in a separate function on purpose,
    because a shape has to stay inert data for two things that come later --
    hashing it into a template-database cache key, and emitting one from a real
    database's statistics. An object that could act would be an object with
    state worth not hashing.

    The seed is part of the declaration rather than an argument to the build,
    for the same reason: two builds of the same shape must produce byte-identical
    databases, and a seed passed at build time would let them differ while the
    declaration claimed they could not.

    A declaration is a :class:`~django_data_shape.table.Table` -- so many rows,
    distributed like this -- or a
    :class:`~django_data_shape.projection.Projection`, which has no row count of
    its own because its cardinality is decided by the tables it copies from. The
    duplicate check below covers both kinds together on purpose: declaring one
    model as a table *and* as a projection is the same over-determination as
    declaring it twice, and it would silently mean whichever the load order
    happened to reach last.
    """

    def __init__(self, *tables: Table | Projection, seed: int = 0) -> None:
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
        self._tables = tables
        self._seed = seed

    # Read-only for the same reason Table's are: a shape has to stay inert,
    # hashable data, and the duplicate-table check above runs once.
    @property
    def tables(self) -> tuple[Table | Projection, ...]:
        return self._tables

    @property
    def seed(self) -> int:
        return self._seed

    def __repr__(self) -> str:
        names = ", ".join(table.model.__name__ for table in self.tables)
        return f"Shape({names}, seed={self.seed})"
