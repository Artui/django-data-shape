from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from functools import partial
from typing import Any, cast

from django_data_shape.declaration.table import Table
from django_data_shape.derivations.derivation import Derivation
from django_data_shape.derivations.scope import Scope
from django_data_shape.distributions.distribution import Distribution
from django_data_shape.keys.key_strategy import KeyStrategy
from django_data_shape.relations.fan_out_plan import FanOutPlan
from django_data_shape.relations.paired import Paired
from django_data_shape.relations.paired_plan import PairedPlan
from django_data_shape.relations.resolve_tree import resolve_tree
from django_data_shape.relations.tree import Tree
from django_data_shape.relations.tree_plan import TreePlan
from django_data_shape.utils import draw, field_stream

# One step of the computation: fill slot ``i`` of the row being built, given the
# row index and the slots filled so far.
_Step = tuple[int, Callable[[int, list[object]], object]]
_Source = Callable[[int, list[object]], object]


def generate_rows(
    table: Table,
    seed: int,
    plans: Mapping[str, FanOutPlan] | None = None,
    pairings: Mapping[str, PairedPlan] | None = None,
) -> Iterator[tuple[Any, ...]]:
    """Yield one tuple per row: the primary key, then each declared column."""
    plans = plans or {}
    pairings = pairings or {}
    keys = table.keys
    key_stream = field_stream(seed, table.db_table, ":key")
    columns = table.columns()
    slot_of = {name: index for index, (name, _field) in enumerate(columns)}

    steps: list[_Step] = [
        (slot_of[name], _producer(table, seed, plans, pairings, name, key_stream))
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
    table: Table,
    seed: int,
    plans: Mapping[str, FanOutPlan],
    pairings: Mapping[str, PairedPlan],
    name: str,
    key_stream: int,
) -> Callable[[int, list[object]], object]:
    """The step for a column that depends on nothing already in the row."""
    declared = table.fields[name]
    if isinstance(declared, Tree):
        tree = resolve_tree(declared, table.rows, seed, table.db_table, name)
        return partial(_from_tree, tree, table.keys, key_stream)
    plan = plans.get(name)
    if plan is not None:
        return partial(_from_plan, plan)
    pairing = pairings.get(name)
    if pairing is not None:
        declared = cast("Paired", table.fields[name])
        return partial(_from_pairing, plans[declared.relation], pairing)
    distribution = cast("Distribution", table.fields[name])
    return partial(_from_distribution, distribution, field_stream(seed, table.db_table, name))


def _derivation_producer(
    table: Table,
    seed: int,
    plans: Mapping[str, FanOutPlan],
    slot_of: Mapping[str, int],
    name: str,
) -> Callable[[int, list[object]], object]:
    """The step for a derived column, with its sources already resolved to readers."""
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
        return partial(_from_group, plans[source])
    return partial(_from_rank, field_stream(seed, table.db_table, f"rank:{source}"))


def _from_distribution(
    distribution: Distribution, stream: int, row: int, values: list[object]
) -> object:
    return distribution.value(row, draw(stream, row))


def _from_plan(plan: FanOutPlan, row: int, values: list[object]) -> object:
    return plan.key_for(row)


def _from_tree(
    plan: TreePlan, keys: KeyStrategy, key_stream: int, row: int, values: list[object]
) -> object:
    parent = plan.parent_for(row)
    return None if parent is None else keys.key_for(parent, key_stream)


def _from_pairing(plan: FanOutPlan, pairing: PairedPlan, row: int, values: list[object]) -> object:
    position, _size = plan.group_position(row)
    return pairing.partner_for(plan.group_of(row), position)


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
