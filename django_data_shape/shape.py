"""A whole declared database."""

from __future__ import annotations

from collections.abc import Sequence

from django_data_shape.check_constraints import check_constraints
from django_data_shape.invalid_shape import InvalidShape
from django_data_shape.invariant import Invariant
from django_data_shape.projection import Projection
from django_data_shape.table import Table


class Shape:
    """The tables to build, and the seed that makes them reproducible.

    A declaration and nothing else: it holds no connection, opens nothing, and
    has no ``build`` method. Building lives in a separate function on purpose,
    because a shape has to stay inert data for two things -- hashing it into a
    template-database cache key, which
    :func:`~django_data_shape.shape_digest.shape_digest` now does, and emitting
    one from a real database's statistics, which is still to come. An object
    that could act would be an object with state worth not hashing.

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

    ``invariants`` are the business rules the loaded data must satisfy, checked
    as SQL at the end of the build and rolling it back if any of them finds a
    row. They are declared on the shape rather than on a table because a rule
    worth writing down often spans two -- a child's tenant matching its
    parent's, two sums agreeing across tables -- and because they all need the
    same thing: every table already loaded.

    **The models' own constraints are read here too**, which is why the
    shape is where the pre-check lives rather than the table. A table knows how
    many rows it declares; only a shape knows how many companies there are, and
    ``one_active_project_per_company permits at most 50,000`` is arithmetic that
    needs both. See
    :func:`~django_data_shape.check_constraints.check_constraints`.
    """

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
        self._tables = tables
        self._seed = seed
        self._invariants = tuple(invariants)

    # Read-only for the same reason Table's are: a shape has to stay inert,
    # hashable data, and the duplicate-table check above runs once.
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
        """The seed and every declaration, keyed by table. See ``Canonical``.

        A mapping rather than a sequence, and it loses nothing: the duplicate
        check above means one table name appears once, and a dict keeps the
        order it was built in. What it buys is that
        :func:`~django_data_shape.shape_digest.shape_digest` can name the table a
        refusal came from, which is the difference between "this shape cannot be
        hashed" and "the compute= on orders.total cannot be hashed".

        The order is **kept, not sorted**, unlike a table's fields. A raw
        ``Projection`` names nothing it reads, so it is ordered after everything
        and several of them fall back to the order they were declared in --
        which means declaration order can reach the data, and a digest that
        sorted it would give two different databases one key.

        **The invariants are in it although not one of them writes a row.**
        A shape that reused another shape's cached template database would never
        run them -- the check happens during the build, and the build is what a
        cache hit skips -- so a rule that made no difference to the key would be
        a rule that silently stopped running the second time. The cost is one
        rebuild for a database that would have been byte-identical, which is the
        cheap side of that trade by a distance.
        """
        return (
            self.seed,
            {table.db_table: table for table in self.tables},
            self._invariants,
        )

    def __repr__(self) -> str:
        names = ", ".join(table.model.__name__ for table in self.tables)
        return f"Shape({names}, seed={self.seed})"
