"""Deciding which table to fill first."""

from __future__ import annotations

from typing import cast

from django.db.models import Model

from django_data_shape.fan_out import FanOut
from django_data_shape.invalid_shape import InvalidShape
from django_data_shape.projection import Projection
from django_data_shape.table import Table


def order_tables(tables: tuple[Table | Projection, ...]) -> tuple[Table | Projection, ...]:
    """Everything a declaration reads, before the declaration itself.

    Not because the database demands it. Django creates every PostgreSQL foreign
    key ``DEFERRABLE INITIALLY DEFERRED``, so inside one transaction the checks
    fire at commit and any order would satisfy them.

    The real reason is that a declaration **reads the tables it depends on**, and
    a table with no rows yet has none to read. A fan-out reads its parent's keys;
    a :class:`~django_data_shape.projection.Projection` selects from two tables
    outright. Ordering is therefore a correctness requirement of this package's
    own design rather than of the schema -- which is worth stating, because the
    schema is where a reader would look for it and would find nothing.

    **A projected table may be a fan-out parent, and that is what this pass is
    for.** A projection runs after the tables it reads, so the temptation is to
    run every projection last and be done; that would quietly forbid a table
    fanning out over a projected one, and forbid it by a scheduling accident
    rather than by anything true about the data. Sorting the whole declaration
    graph at once costs nothing here and removes the restriction: a projection
    is just another node with edges to what it reads.

    A statement this package did not write -- ``Projection(..., sql=...)`` --
    names nothing it reads, because nothing here parses SQL. It is ordered after
    every table and every derived projection instead, which is the only safe
    reading of "this could select from anything". Two of them have no edge
    between them and keep the order they were declared in.

    A cycle is refused by name. Two declarations that each read the other cannot
    both be filled second, and no amount of deferring changes that.
    """
    by_model: dict[type[Model], Table | Projection] = {table.model: table for table in tables}
    ordered: list[Table | Projection] = []
    state: dict[Table | Projection, int] = {}

    def visit(table: Table | Projection, trail: tuple[Table | Projection, ...]) -> None:
        mark = state.get(table, 0)
        if mark == 2:
            return
        if mark == 1:
            names = " -> ".join(t.model.__name__ for t in (*trail, table))
            raise InvalidShape(
                f"These tables read each other in a cycle: {names}. One of them has to be "
                "filled first, and a cycle means neither can be."
            )
        state[table] = 1
        # No self-edge to skip. A self-referential ``FanOut`` is refused by
        # ``Table``, a projection reading the table it fills is refused by
        # ``Projection``, and a raw projection is left out of its own dependency
        # list below -- so guarding against one here would be a branch no
        # declaration can reach. The day self-referential trees arrive, this is
        # the line that has to learn about them.
        for dependency in _dependencies(table, by_model, tables):
            visit(dependency, (*trail, table))
        state[table] = 2
        ordered.append(table)

    for table in tables:
        visit(table, ())
    return tuple(ordered)


def _dependencies(
    table: Table | Projection,
    by_model: dict[type[Model], Table | Projection],
    everything: tuple[Table | Projection, ...],
) -> list[Table | Projection]:
    """The declarations ``table`` has to be filled after.

    A model that is not in the shape is a table the caller built themselves,
    which is the supported hybrid: the ORM for the small tables, this package
    for the large ones. Its rows are read when the time comes, and nothing here
    has to order it.
    """
    if isinstance(table, Projection):
        if not table.reads:
            return [other for other in everything if other is not table and not _is_opaque(other)]
        return [by_model[model] for model in table.reads if model in by_model]

    parents: list[Table | Projection] = []
    for name, declared in table.fields.items():
        if not isinstance(declared, FanOut):
            continue
        # cast, not a guard: related_model is Optional on the generic field
        # descriptor, but Table has already refused a FanOut on anything that
        # is not a relation. Branching on None here would add a path no test
        # could reach, which is how 100% branch coverage stops being achievable
        # honestly.
        parent = cast("type[Model]", table.model._meta.get_field(name).related_model)
        if parent in by_model:
            parents.append(by_model[parent])
    return parents


def _is_opaque(table: Table | Projection) -> bool:
    """Whether this declaration's inputs are unknowable from the declaration."""
    return isinstance(table, Projection) and not table.reads
