"""Turning a declared table into the tuples COPY will consume."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from functools import partial
from typing import Any, cast

from django_data_shape.derivations.derivation import Derivation
from django_data_shape.derivations.scope import Scope
from django_data_shape.distributions.distribution import Distribution
from django_data_shape.fan_out_plan import FanOutPlan
from django_data_shape.table import Table
from django_data_shape.utils import draw, field_stream

# One step of the computation: fill slot ``i`` of the row being built, given the
# row index and the slots filled so far.
_Step = tuple[int, Callable[[int, list[object]], object]]
_Source = Callable[[int, list[object]], object]


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

    **Columns are computed in one order and emitted in another.** The emitted
    order is ``columns()``, sorted by name, because it is the ``COPY`` column
    list and has to be stable. The computed order puts every derivation after
    the columns it reads, which sorting by name would only satisfy by accident.
    Conflating the two is the mistake this design keeps meeting under different
    names, so the two orders are built separately here and never inferred from
    each other.

    A generator rather than a list: a million tuples is real memory, psycopg
    writes them one at a time anyway, and materialising the set would buy
    nothing but peak RSS.
    """
    plans = plans or {}
    keys = table.keys
    key_stream = field_stream(seed, table.db_table, ":key")
    columns = table.columns()
    slot_of = {name: index for index, (name, _field) in enumerate(columns)}

    # Every column is reduced to one callable before the loop starts. At a
    # million rows the loop body runs a million times per column, so which kind
    # of column this is, and where each of its sources comes from, are decided
    # once rather than a million times.
    steps: list[_Step] = [
        (slot_of[name], _producer(table, seed, plans, name))
        for name, _field in columns
        if name not in table.computation_order()
    ]
    steps.extend(
        (slot_of[name], _derivation_producer(table, seed, plans, slot_of, name))
        for name in table.computation_order()
    )

    for row in range(table.rows):
        values: list[object] = [None] * len(columns)
        for slot, produce in steps:
            values[slot] = produce(row, values)
        yield (keys.key_for(row, key_stream), *values)


def _producer(
    table: Table, seed: int, plans: Mapping[str, FanOutPlan], name: str
) -> Callable[[int, list[object]], object]:
    """The step for a column that depends on nothing already in the row."""
    plan = plans.get(name)
    if plan is not None:
        return partial(_from_plan, plan)
    distribution = cast("Distribution", table.fields[name])
    return partial(_from_distribution, distribution, field_stream(seed, table.db_table, name))


def _derivation_producer(
    table: Table,
    seed: int,
    plans: Mapping[str, FanOutPlan],
    slot_of: Mapping[str, int],
    name: str,
) -> Callable[[int, list[object]], object]:
    """The step for a derived column, with its sources already resolved to readers.

    This is where the scope parameter is spent, and it is spent once per column
    rather than once per row. Everything downstream -- the derivation itself, the
    ordering, the guard around generation -- is scope-blind, which is what makes
    the four faces one mechanism rather than four.
    """
    derivation = cast("Derivation", table.fields[name])
    readers: tuple[_Source, ...] = tuple(
        _source(table, seed, plans, slot_of, derivation.scope, source)
        for source in derivation.sources
    )
    return partial(_from_derivation, derivation, field_stream(seed, table.db_table, name), readers)


def _source(
    table: Table,
    seed: int,
    plans: Mapping[str, FanOutPlan],
    slot_of: Mapping[str, int],
    scope: Scope,
    source: str,
) -> _Source:
    """One resolved source: where this name is read from, in this scope."""
    if scope is Scope.ROW:
        return partial(_from_row, slot_of[source])
    if scope is Scope.PARENT:
        relation, _, field_name = source.partition(".")
        return partial(_from_parent, plans[relation], field_name)
    if scope is Scope.GROUP:
        # The plan again, and a third question asked of the same partition:
        # which parent (PARENT), which of its columns (PARENT), and now where in
        # its range this row sits. All three are O(1) because the fan-out is a
        # partition rather than a per-child draw.
        return partial(_from_group, plans[source])
    # Scope.RANK. A rank is a name the declaration invented, not a column, so
    # its stream is namespaced away from the field streams -- otherwise a rank
    # called "total" would silently be the "total" column's own draw.
    return partial(_from_rank, field_stream(seed, table.db_table, f"rank:{source}"))


def _from_distribution(
    distribution: Distribution, stream: int, row: int, values: list[object]
) -> object:
    return distribution.value(row, draw(stream, row))


def _from_plan(plan: FanOutPlan, row: int, values: list[object]) -> object:
    return plan.key_for(row)


def _from_derivation(
    derivation: Derivation,
    stream: int,
    readers: tuple[_Source, ...],
    row: int,
    values: list[object],
) -> object:
    return derivation.value(row, draw(stream, row), tuple(read(row, values) for read in readers))


def _from_row(slot: int, row: int, values: list[object]) -> object:
    return values[slot]


def _from_parent(plan: FanOutPlan, field: str, row: int, values: list[object]) -> object:
    return plan.parent_value(field, row)


def _from_group(plan: FanOutPlan, row: int, values: list[object]) -> object:
    return plan.group_position(row)


def _from_rank(stream: int, row: int, values: list[object]) -> object:
    return draw(stream, row)
