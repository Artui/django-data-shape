"""Turning a declared table into the tuples COPY will consume."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from functools import partial
from typing import Any, cast

from django_data_shape.distributions.distribution import Distribution
from django_data_shape.fan_out_plan import FanOutPlan
from django_data_shape.table import Table
from django_data_shape.utils import draw, field_stream


def generate_rows(
    table: Table, seed: int, plans: Mapping[str, FanOutPlan] | None = None
) -> Iterator[tuple[Any, ...]]:
    """Yield one tuple per row: the primary key, then each declared column.

    Rows, not model instances. The ORM is the wrong tool at these counts -- the
    difference between a load measured in seconds and one measured in minutes --
    and nothing here needs an instance, because no ``save`` will run and no
    signal should fire.

    Primary keys come from the table's key strategy, which is a deterministic
    function of the row index -- ``row + 1`` for an integer key, a derived UUID
    for a UUID one, a caller's own function for anything else. Determinism is the
    requirement, not the integers: it is what lets a child compute its parent's
    key without a lookup, and what makes a self-referential tree acyclic on the
    index rather than on the value.

    ``plans`` carries the resolved fan-out for each relation column. Resolving
    happens outside this function because it has to read the parent's real keys
    out of the database, and keeping the query there leaves generation itself
    backend-neutral and testable without a connection.

    A generator rather than a list: a million tuples is real memory, psycopg
    writes them one at a time anyway, and materialising the set would buy
    nothing but peak RSS.
    """
    plans = plans or {}
    keys = table.keys
    key_stream = field_stream(seed, table.db_table, ":key")
    # Each column is reduced to one callable of the row index before the loop
    # starts. At a million rows the loop body runs a million times per column, so
    # the branch between a fan-out and a value distribution is worth deciding
    # once rather than a million times.
    emit: list[Callable[[int], object]] = []
    for name, _field in table.columns():
        plan = plans.get(name)
        if plan is not None:
            emit.append(plan.key_for)
            continue
        distribution = cast("Distribution", table.fields[name])
        emit.append(
            partial(_from_distribution, distribution, field_stream(seed, table.db_table, name))
        )

    for row in range(table.rows):
        yield (keys.key_for(row, key_stream), *(produce(row) for produce in emit))


def _from_distribution(distribution: Distribution, stream: int, row: int) -> object:
    return distribution.value(row, draw(stream, row))
