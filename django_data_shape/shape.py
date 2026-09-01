"""A whole declared database."""

from __future__ import annotations

from django_data_shape.invalid_shape import InvalidShape
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
    """

    def __init__(self, *tables: Table, seed: int = 0) -> None:
        if not tables:
            raise InvalidShape("A shape needs at least one table.")
        seen: dict[str, Table] = {}
        for table in tables:
            key = table.db_table
            if key in seen:
                raise InvalidShape(
                    f"{key} is declared twice. One table gets one row count and one set of "
                    "distributions; two declarations would silently mean whichever came last."
                )
            seen[key] = table
        self.tables = tables
        self.seed = seed

    def __repr__(self) -> str:
        names = ", ".join(table.model.__name__ for table in self.tables)
        return f"Shape({names}, seed={self.seed})"
