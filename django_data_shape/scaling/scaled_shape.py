from __future__ import annotations

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.declaration.projection import Projection
from django_data_shape.declaration.shape import Shape
from django_data_shape.declaration.table import Table


def scaled_shape(shape: Shape, factor: int) -> Shape:
    """``shape`` with every declared row count multiplied by ``factor``."""
    if not isinstance(factor, int) or factor < 1:
        raise InvalidShape(
            f"A scale factor multiplies every declared row count, so it has to be a whole "
            f"number of at least 1; got {factor!r}. A world smaller than the declaration is "
            "the declaration written smaller: growth is asserted by scaling up from the "
            "cheapest world that still means something, never by shrinking a larger one."
        )

    try:
        tables = tuple(
            table.scaled(factor)
            if isinstance(table, Projection)
            else Table(
                table.model,
                rows=table.rows * factor,
                fields=dict(table.fields),
                keys=table.keys,
                statistics=dict(table.statistics),
            )
            for table in shape.tables
        )
    except InvalidShape as invalid:
        raise InvalidShape(f"At scale factor {factor}: {invalid}") from invalid

    return Shape(*tables, seed=shape.seed, invariants=tuple(shape.invariants))
