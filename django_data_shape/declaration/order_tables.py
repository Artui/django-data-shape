from __future__ import annotations

from typing import cast

from django.db.models import Model

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.declaration.projection import Projection
from django_data_shape.declaration.table import Table
from django_data_shape.relations.fan_out import FanOut


def order_tables(tables: tuple[Table | Projection, ...]) -> tuple[Table | Projection, ...]:
    """Everything a declaration reads, before the declaration itself."""
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
        # No self-edge to skip: Table and Projection refuse self-reads, so a guard is unreachable.
        for dependency in _dependencies(table, by_model):
            visit(dependency, (*trail, table))
        state[table] = 2
        ordered.append(table)

    for table in tables:
        if not _is_opaque(table):
            visit(table, ())
    for table in tables:
        visit(table, ())
    return tuple(ordered)


def _dependencies(
    table: Table | Projection, by_model: dict[type[Model], Table | Projection]
) -> list[Table | Projection]:
    """The declarations ``table`` says it has to be filled after."""
    if isinstance(table, Projection):
        return [by_model[model] for model in table.reads if model in by_model]

    parents: list[Table | Projection] = []
    for name, declared in table.fields.items():
        if not isinstance(declared, FanOut):
            continue
        parent = cast("type[Model]", table.model._meta.get_field(name).related_model)
        if parent in by_model:
            parents.append(by_model[parent])
    return parents


def _is_opaque(table: Table | Projection) -> bool:
    """Whether this declaration's inputs are unknowable from the declaration."""
    return isinstance(table, Projection) and not table.reads
