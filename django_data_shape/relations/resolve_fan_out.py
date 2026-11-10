from __future__ import annotations

import math
import numbers
from decimal import Decimal
from typing import Any, cast

from django.db.models import Model

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.relations.fan_out import FanOut
from django_data_shape.relations.fan_out_plan import FanOutPlan
from django_data_shape.utils import draw, field_stream, named_keys


def resolve_fan_out(
    fan_out: FanOut,
    parent: type[Model],
    rows: int,
    seed: int,
    table: str,
    field: str,
    connection: Any,
    parent_fields: tuple[str, ...] = (),
) -> FanOutPlan:
    """Read the parent's real keys and partition ``rows`` children across them."""
    named = named_keys(fan_out.parents, parent, table, field)
    keys, parent_values = _read_parents(parent, parent_fields, connection, named)
    _require_every_named_parent(named, keys, parent, table, field)
    keys, parent_values = _in_declared_order(named, keys, parent_values, parent)

    if not keys and rows:
        raise InvalidShape(
            f"{table}.{field} fans out over {parent.__name__}, which has no rows. "
            "Load the parent first, or declare it in the same shape so it is built before "
            "this table."
        )

    sizes = _sizes(fan_out, keys, rows, seed, table, field)
    starts: list[int] = []
    running = 0
    for size in sizes:
        starts.append(running)
        running += size

    return FanOutPlan(
        keys=keys,
        starts=starts,
        rows=rows,
        null_stream=field_stream(seed, table, f"{field}:null"),
        null_share=fan_out.null,
        interleave=fan_out.placement == "arrival",
        parent_values=parent_values,
    )


def _in_declared_order(
    named: tuple[object, ...] | None,
    keys: list[int],
    parent_values: dict[str, list[object]],
    parent: type[Model],
) -> tuple[list[int], dict[str, list[object]]]:
    """Put named parents back in the order the declaration named them."""
    if named is None:
        return keys, parent_values
    to_python = parent._meta.pk.to_python
    position: dict[object, int] = {to_python(key): index for index, key in enumerate(keys)}
    order = [position[key] for key in named]
    return (
        [keys[index] for index in order],
        {name: [values[index] for index in order] for name, values in parent_values.items()},
    )


def _require_every_named_parent(
    named: tuple[object, ...] | None,
    keys: list[int],
    parent: type[Model],
    table: str,
    field: str,
) -> None:
    """Every key ``parents=`` named has to be a row, or the narrowing is a lie."""
    if named is None:
        return
    present = {parent._meta.pk.to_python(key) for key in keys}
    missing = [key for key in named if key not in present]
    if not missing:
        return
    raise InvalidShape(
        f"{table}.{field} fans out over {parent.__name__} and names "
        f"{', '.join(repr(key) for key in missing)} in parents=, which "
        f"{'is not a row' if len(missing) == 1 else 'are not rows'} of "
        f"{parent._meta.db_table}. A key that matches nothing is not an empty share -- the rows "
        "that would have gone to it go to the other parents named instead, and to none at all if "
        "it was the only one, so the world would be built and quietly not be the declared one. "
        "The usual causes are a key from another test, a factory row that was rolled back, and a "
        "list built from a queryset filtered differently from the one you meant. Inside "
        "scaled_world there is one more: a world empties the tables its shape declares before "
        f"building, so if {parent._meta.db_table} is declared in the same shape, a key from a row "
        "made before the world is gone by the time this fan-out reads it."
    )


def _read_parents(
    parent: type[Model],
    parent_fields: tuple[str, ...],
    connection: Any,
    named: tuple[object, ...] | None = None,
) -> tuple[list[int], dict[str, list[object]]]:
    """The parent's keys, and any of its columns a child derives from."""
    if not parent_fields:
        pk_column = parent._meta.pk.column
        quote = connection.ops.quote_name
        where = (
            ""
            if named is None
            else f" WHERE {quote(pk_column)} IN ({', '.join(['%s'] * len(named))})"
        )
        params = (
            None
            if named is None
            else [parent._meta.pk.get_db_prep_value(key, connection) for key in named]
        )
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {quote(pk_column)} FROM {quote(parent._meta.db_table)}"
                f"{where} ORDER BY {quote(pk_column)}",
                params,
            )
            return [row[0] for row in cursor.fetchall()], {}

    queryset = parent._base_manager.using(connection.alias)
    if named is not None:
        queryset = queryset.filter(pk__in=list(named))
    records = list(queryset.order_by("pk").values_list("pk", *parent_fields))
    return (
        [record[0] for record in records],
        {
            name: [record[index + 1] for record in records]
            for index, name in enumerate(parent_fields)
        },
    )


def _sizes(
    fan_out: FanOut, keys: list[int], rows: int, seed: int, table: str, field: str
) -> list[int]:
    """How many children each parent gets, summing to exactly ``rows``."""
    if rows == 0:
        return [0] * len(keys)

    weight_stream = field_stream(seed, table, f"{field}:weight")
    childless_stream = field_stream(seed, table, f"{field}:childless")

    weights: list[float] = []
    for index in range(len(keys)):
        if fan_out.childless and draw(childless_stream, index) < fan_out.childless:
            weights.append(0.0)
            continue
        weight = fan_out.sizes.value(index, draw(weight_stream, index))
        if isinstance(weight, bool) or not isinstance(weight, (numbers.Real, Decimal)):
            raise InvalidShape(
                f"{table}.{field} needs numeric fan-out sizes, but "
                f"{fan_out.sizes!r} produced {weight!r}. A fan-out size is a count of children "
                "per parent, so the distribution weighing it has to produce numbers -- int, "
                "float, Decimal and Fraction all work, and the weights are normalised so their "
                "scale does not matter."
            )
        value = float(weight)
        if value < 0 or not math.isfinite(value):
            raise InvalidShape(
                f"{table}.{field} needs fan-out sizes of zero or more, finite, but "
                f"{fan_out.sizes!r} produced {weight!r}. A fan-out size weighs how many "
                "children a parent gets, so it cannot be negative or infinite; childless= is "
                "how a parent with none is asked for."
            )
        weights.append(value)

    total = sum(weights)
    if not math.isfinite(total):
        raise InvalidShape(
            f"{table}.{field} draws fan-out sizes from {fan_out.sizes!r} whose sum over "
            f"{len(keys)} parents is too large to divide by. The sizes are normalised, so "
            "their scale does not matter: declare the same distribution with smaller numbers."
        )
    if total <= 0:
        raise InvalidShape(
            f"{table}.{field} gives all {len(keys)} of its parents a weight of zero, so there "
            f"is nowhere to put {rows} rows. Lower childless, or widen the size distribution."
        )

    exact = (
        [weight / total * rows for weight in weights]
        if fan_out.max_children is None
        else _water_filled(weights, total, rows, fan_out, table, field)
    )
    sizes = [int(value) for value in exact]
    remainder = rows - sum(sizes)
    order = sorted(range(len(sizes)), key=lambda i: exact[i] - sizes[i], reverse=True)
    for index in order[:remainder]:
        sizes[index] += 1
    return sizes


def _water_filled(
    weights: list[float], total: float, rows: int, fan_out: FanOut, table: str, field: str
) -> list[float]:
    """Each parent's exact share of ``rows`` when no parent may exceed ``cap``."""
    cap = cast("int", fan_out.max_children)
    holding = [index for index, weight in enumerate(weights) if weight > 0]
    if rows > cap * len(holding):
        remedies = (
            "Raise max_children, lower childless, or declare fewer rows."
            if fan_out.childless
            else "Raise max_children or declare fewer rows."
        )
        raise InvalidShape(
            f"{table}.{field} declares max_children={cap}, and {len(holding)} of its "
            f"{len(weights)} parents have a non-zero weight, so at most {cap * len(holding)} rows "
            f"fit; it declares {rows}. {remedies}"
        )

    exact = [0.0] * len(weights)
    heaviest_first = sorted(holding, key=lambda index: weights[index], reverse=True)
    left = [0.0] * (len(heaviest_first) + 1)
    for position in range(len(heaviest_first) - 1, -1, -1):
        left[position] = left[position + 1] + weights[heaviest_first[position]]
    left[0] = total
    count, free_rows = 0, rows
    while weights[heaviest_first[count]] / left[count] * free_rows > cap:
        exact[heaviest_first[count]] = float(cap)
        free_rows -= cap
        count += 1
    for rest in heaviest_first[count:]:
        exact[rest] = weights[rest] / left[count] * free_rows
    return exact
