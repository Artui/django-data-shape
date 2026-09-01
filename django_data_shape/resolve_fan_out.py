"""Turning a declared fan-out into a partition over the parent keys that exist."""

from __future__ import annotations

from typing import Any

from django.db.models import Model

from django_data_shape.fan_out import FanOut
from django_data_shape.fan_out_plan import FanOutPlan
from django_data_shape.invalid_shape import InvalidShape
from django_data_shape.utils import draw, field_stream


def resolve_fan_out(
    fan_out: FanOut,
    parent: type[Model],
    rows: int,
    seed: int,
    table: str,
    field: str,
    connection: Any,
) -> FanOutPlan:
    """Read the parent's real keys and partition ``rows`` children across them.

    The keys are **queried, not assumed**. An earlier design took them to be
    the dense ``1..N`` range this package assigns, which is wrong in the case
    that matters most: a project builds its fifty companies with the ORM -- where
    the row count is small and the ORM is the right tool -- and asks this package
    only for the two million orders. Their keys are then whatever the sequence
    handed out, and a child pointing at ``1..50`` would point at nothing.

    Reading them is also what makes referential integrity hold by construction
    rather than by validation: every key emitted came out of the parent table.
    """
    pk_column = parent._meta.pk.column
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {quote(pk_column)} FROM {quote(parent._meta.db_table)} "
            f"ORDER BY {quote(pk_column)}"
        )
        keys = [row[0] for row in cursor.fetchall()]

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
    )


def _sizes(
    fan_out: FanOut, keys: list[int], rows: int, seed: int, table: str, field: str
) -> list[int]:
    """How many children each parent gets, summing to exactly ``rows``.

    The largest-remainder method rather than plain rounding, because rounding
    each share independently does not add up: a thousand parents rounded down
    lose hundreds of rows, and a partition that does not cover the range would
    leave children pointing past the end of it.
    """
    # Nothing to place means nothing to weigh. Reaching the zero-total refusal
    # below with an empty table would refuse a shape that is merely empty, which
    # is a legitimate thing to declare -- it is what a parent with no children
    # looks like.
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
        # Checked rather than coerced. A size distribution handing back
        # something that is not a number is a declaration mistake, and the
        # alternative is a TypeError from inside the partition arithmetic that
        # names neither the table nor the field.
        if not isinstance(weight, (int, float)) or isinstance(weight, bool):
            raise InvalidShape(
                f"{table}.{field} needs numeric fan-out sizes, but "
                f"{fan_out.sizes!r} produced {weight!r}."
            )
        weights.append(float(weight))

    total = sum(weights)
    if total <= 0:
        raise InvalidShape(
            f"{table}.{field} gives all {len(keys)} of its parents a weight of zero, so there "
            f"is nowhere to put {rows} rows. Lower childless, or widen the size distribution."
        )

    exact = [weight / total * rows for weight in weights]
    sizes = [int(value) for value in exact]
    remainder = rows - sum(sizes)
    # Hand the leftover rows to the parents with the largest fractional parts:
    # the ones that were closest to earning another row.
    order = sorted(range(len(sizes)), key=lambda i: exact[i] - sizes[i], reverse=True)
    for index in order[:remainder]:
        sizes[index] += 1
    return sizes
