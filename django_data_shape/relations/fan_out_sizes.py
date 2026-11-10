from __future__ import annotations

from typing import cast

from django.db import DEFAULT_DB_ALIAS, connections
from django.db.models import Field, Model

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.declaration.shape import Shape
from django_data_shape.declaration.table import Table
from django_data_shape.relations.fan_out import FanOut
from django_data_shape.relations.resolve_fan_out import resolve_fan_out
from django_data_shape.relations.world_changed import WorldChanged
from django_data_shape.types.children_per_parent import ChildrenPerParent


def fan_out_sizes(
    shape: Shape, model: type[Model], field: str, *, using: str = DEFAULT_DB_ALIAS
) -> ChildrenPerParent:
    """How many children each parent got, for one declared fan-out."""
    declared = {table.db_table: table for table in shape.tables}

    declaration = declared.get(model._meta.db_table)
    if declaration is None:
        raise InvalidShape(
            f"This shape declares no table for {model.__name__}, so it has no fan-out over "
            f"{field} to report. It declares: "
            f"{', '.join(sorted(declared)) or 'nothing'}."
        )
    if not isinstance(declaration, Table):
        raise InvalidShape(
            f"{model.__name__} is declared as a Projection, whose foreign keys are copied along "
            "the join it selects from rather than spread by a fan-out. There is no partition to "
            "invert; the counts it produces are a property of the tables it reads."
        )

    fan_out = declaration.fields.get(field)
    if not isinstance(fan_out, FanOut):
        raise InvalidShape(
            f"{model.__name__}.{field} is not declared with a FanOut in this shape, and only a "
            "fan-out partitions children across parents. Its declared fields are: "
            f"{', '.join(sorted(declaration.fields)) or 'none'}."
        )

    relation = dict(declaration.relations())[field]
    parent = cast("type[Model]", cast("Field[object, object]", relation).related_model)

    parent_declaration = (
        None if fan_out.parents is not None else declared.get(parent._meta.db_table)
    )
    try:
        plan = resolve_fan_out(
            fan_out,
            parent,
            declaration.rows,
            shape.seed,
            declaration.db_table,
            field,
            connections[using],
        )
    except InvalidShape as refused:
        if isinstance(parent_declaration, Table):
            holds = parent._base_manager.using(using).count()
            if holds != parent_declaration.rows:
                raise _world_changed(
                    parent, holds, parent_declaration.rows, model, field
                ) from refused
        raise
    keys = plan.parent_keys()

    if isinstance(parent_declaration, Table) and len(keys) != parent_declaration.rows:
        raise _world_changed(parent, len(keys), parent_declaration.rows, model, field)

    return ChildrenPerParent(keys, plan.sizes(), fan_out.null)


def _world_changed(
    parent: type[Model], holds: int, declares: int, model: type[Model], field: str
) -> WorldChanged:
    return WorldChanged(
        f"{parent.__name__} holds {holds} rows and this shape declares "
        f"{declares}, so the fan-out for {model.__name__}.{field} would be "
        "spread over parents the children were never spread across. The counts are derived "
        "from the parent keys rather than counted off the child table, so an answer here "
        "would describe a world that was never built. The usual causes are a test that "
        "created more parents before asking, and a transactional test that truncated the "
        "tables a session-scoped fixture built."
    )
