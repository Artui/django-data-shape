"""Partitioning children over the parent keys that actually exist."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from typing import Any

import pytest

from django_data_shape import Constant, FanOut, InvalidShape, Uniform, Zipf
from django_data_shape.resolve_fan_out import resolve_fan_out
from tests.testapp.models import Company


class _Cursor:
    def __init__(self, keys: list[int]) -> None:
        self._keys = keys

    def execute(self, sql: str, params: Any = None) -> None:
        self.sql = sql

    def fetchall(self) -> list[tuple[int]]:
        return [(key,) for key in self._keys]


class _Connection:
    """Everything resolve_fan_out touches, and nothing else.

    It reads the parent's keys with one query and is otherwise pure arithmetic,
    so a stub covers every branch of the partition without a database. That is
    the same reason the backend gate reads a vendor: logic reachable only
    through a real connection is logic the coverage gate cannot see.
    """

    def __init__(self, keys: list[int]) -> None:
        self._keys = keys
        self.ops = type("Ops", (), {"quote_name": staticmethod(lambda name: f'"{name}"')})()

    @contextmanager
    def cursor(self) -> Any:
        yield _Cursor(self._keys)


def _resolve(keys: list[int], rows: int, fan_out: FanOut | None = None) -> Any:
    return resolve_fan_out(
        fan_out or FanOut(Zipf(1.2)),
        Company,
        rows,
        seed=7,
        table="testapp_session",
        field="company",
        connection=_Connection(keys),
    )


def test_the_partition_covers_every_child_exactly_once() -> None:
    plan = _resolve(list(range(100, 150)), rows=1000)

    # Sums to the declared row count, not approximately: a partition that did
    # not cover the range would leave children pointing past the end of it, and
    # rounding each parent's share independently loses hundreds of rows.
    assert sum(plan.sizes()) == 1000


def test_keys_come_from_the_parent_rather_than_being_assumed() -> None:
    # The correction that prompted this design. An earlier version took parents
    # to be the dense 1..N range this package assigns, which is wrong the moment
    # the parent was built by the ORM -- the realistic hybrid, where small
    # tables come from a service and the large ones from here.
    keys = [7, 41, 9001, 12345]
    plan = _resolve(keys, rows=200)

    emitted = {plan.key_for(row) for row in range(200)}
    assert emitted <= set(keys)
    assert min(emitted) >= 7


def test_a_zipf_fan_out_gives_a_head_and_a_tail() -> None:
    sizes = sorted(_resolve(list(range(1, 201)), rows=20_000).sizes(), reverse=True)

    # The whole point: uniform fan-out makes the planner's average the truth, so
    # a join estimate cannot miss. A head and a tail is what production has.
    assert sizes[0] > 10 * sizes[len(sizes) // 2]


def test_the_childless_tail_is_representable() -> None:
    plan = _resolve(list(range(1, 101)), rows=5000, fan_out=FanOut(Zipf(), childless=0.4))
    sizes = plan.sizes()

    # The case hand-written fixtures always omit, and the one that decides
    # whether an inner and an outer join return the same thing.
    assert 25 <= sizes.count(0) <= 55
    assert sum(sizes) == 5000


def test_a_null_share_thins_the_children() -> None:
    plan = _resolve(list(range(1, 51)), rows=4000, fan_out=FanOut(Zipf(), null=0.25))
    keys = [plan.key_for(row) for row in range(4000)]
    nulls = sum(1 for key in keys if key is None)

    # null_frac is planner-visible, so an optional foreign key that is never
    # null is its own kind of unrealistic.
    assert 850 < nulls < 1150


def test_arrival_and_grouped_place_the_same_children_differently() -> None:
    keys = list(range(1, 21))
    grouped = _resolve(keys, rows=400, fan_out=FanOut(Uniform(1, 2), placement="grouped"))
    arrival = _resolve(keys, rows=400, fan_out=FanOut(Uniform(1, 2), placement="arrival"))

    # Same partition, different emission order. Grouped walks each parent's
    # children contiguously; arrival interleaves them the way rows really
    # arrive. The per-parent counts are identical either way.
    assert Counter(grouped.key_for(r) for r in range(400)) == Counter(
        arrival.key_for(r) for r in range(400)
    )
    first_ten_grouped = [grouped.key_for(r) for r in range(10)]
    first_ten_arrival = [arrival.key_for(r) for r in range(10)]
    assert len(set(first_ten_grouped)) < len(set(first_ten_arrival))


def test_a_parent_with_no_rows_is_refused_by_name() -> None:
    with pytest.raises(InvalidShape, match="which has no rows"):
        _resolve([], rows=10)


def test_no_parents_and_no_children_is_fine() -> None:
    assert _resolve([], rows=0).sizes() == []


def test_every_parent_weighted_to_zero_is_refused() -> None:
    with pytest.raises(InvalidShape, match="weight of zero"):
        _resolve(list(range(1, 11)), rows=100, fan_out=FanOut(Constant(0)))


def test_a_non_numeric_size_is_refused_and_names_the_field() -> None:
    with pytest.raises(InvalidShape, match="numeric fan-out sizes") as raised:
        _resolve(list(range(1, 11)), rows=100, fan_out=FanOut(Constant("many")))

    assert "testapp_session.company" in str(raised.value)


def test_a_rows_position_in_its_group_and_the_groups_size_are_arithmetic() -> None:
    # The property the whole per-group primitive rests on: nothing is buffered,
    # nothing is sorted, and the answer for one row does not depend on any
    # other. Under grouped placement the slots are the row indices, so the
    # partition is readable straight off the sizes.
    plan = _resolve([10, 20, 30], rows=6, fan_out=FanOut(Constant(1), placement="grouped"))

    assert plan.sizes() == [2, 2, 2]
    assert [plan.group_position(row) for row in range(6)] == [
        (0, 2),
        (1, 2),
        (0, 2),
        (1, 2),
        (0, 2),
        (1, 2),
    ]


def test_every_group_is_covered_exactly_once_under_arrival_placement() -> None:
    # The half that matters, because arrival is the honest default: the rows of
    # one group are scattered through the table on purpose, and every position
    # of every group still gets exactly one row.
    keys = list(range(100, 120))
    plan = _resolve(keys, rows=500)
    sizes = plan.sizes()

    seen: dict[int, set[int]] = {}
    for row in range(500):
        position, size = plan.group_position(row)
        parent = plan.key_for(row)
        assert parent is not None
        assert size == sizes[keys.index(parent)]
        assert position not in seen.setdefault(parent, set())
        seen[parent].add(position)

    for parent, positions in seen.items():
        assert positions == set(range(sizes[keys.index(parent)]))


def test_a_childless_parent_is_never_the_group_a_row_is_attributed_to() -> None:
    # Empty ranges share a start with the next parent, and bisect_right steps
    # past every one of them -- so a size of zero is unreachable rather than
    # special-cased, and no row can be handed a group it is the only member of
    # by accident.
    plan = _resolve(list(range(100, 140)), rows=200, fan_out=FanOut(Zipf(1.4), childless=0.5))

    assert 0 in plan.sizes()
    assert all(plan.group_position(row)[1] > 0 for row in range(200))
