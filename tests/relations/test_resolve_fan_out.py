"""Partitioning children over the parent keys that actually exist."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from django_data_shape import Constant, FanOut, InvalidShape, Uniform, Zipf
from django_data_shape.relations.resolve_fan_out import resolve_fan_out
from tests.testapp.models import Company


class _Cursor:
    def __init__(self, keys: list[int]) -> None:
        self._keys = keys

    def execute(self, sql: str, params: Any = None) -> None:
        self.sql = sql
        self.params = params

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
        self.last_sql = ""
        self.last_params: Any = None
        self.ops = type(
            "Ops",
            (),
            {
                "quote_name": staticmethod(lambda name: f'"{name}"'),
                # Both reached through the key field's get_db_prep_value, which
                # is how a named key is sent; an integer key goes out unchanged.
                "adapt_integerfield_value": staticmethod(lambda value, internal_type: value),
                "validate_autopk_value": staticmethod(lambda value: value),
            },
        )()

    @contextmanager
    def cursor(self) -> Any:
        cursor = _Cursor(self._keys)
        yield cursor
        self.last_sql = cursor.sql
        self.last_params = cursor.params


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


def test_parents_narrows_which_keys_the_partition_covers() -> None:
    # The narrowing is done by the database -- the statement carries the keys as
    # parameters -- so what the stub hands back is what a server would have. What
    # is checked here is that the partition covers exactly those.
    plan = _resolve([20, 40], rows=2, fan_out=FanOut(Constant(1), parents=[20, 40]))

    assert plan.parent_keys() == (20, 40)


def test_the_statement_asks_for_only_the_named_parents() -> None:
    connection = _Connection([20, 40])

    resolve_fan_out(
        FanOut(Constant(1), parents=[20, 40]),
        Company,
        2,
        seed=7,
        table="t",
        field="company",
        connection=connection,
    )

    assert "IN (%s, %s)" in connection.last_sql
    assert connection.last_params == [20, 40]


def test_a_parent_named_in_another_spelling_of_its_key_is_that_parent() -> None:
    # The keys come back as the primary key's own type, so a key named as a
    # string -- read from JSON, passed between processes -- has to be read as
    # that type too before it is compared, and before it is sent.
    connection = _Connection([20, 40])

    plan = resolve_fan_out(
        FanOut(Constant(1), parents=["40", "20"]),
        Company,
        2,
        seed=7,
        table="t",
        field="company",
        connection=connection,
    )

    assert plan.parent_keys() == (40, 20)
    assert connection.last_params == [40, 20]


def test_a_named_parent_that_is_not_there_is_refused_by_key() -> None:
    # The failure this has to catch loudly: a caller passes a primary key from
    # another database, another test, or a factory whose row was rolled back.
    # Every child that would have pointed at it silently goes somewhere else --
    # or the table is empty, if it was the only key named.
    with pytest.raises(InvalidShape) as raised:
        _resolve([20], rows=2, fan_out=FanOut(Constant(1), parents=[20, 99]))

    message = str(raised.value)
    assert "99" in message
    assert "Company" in message


def test_the_refusal_names_every_missing_key_and_not_only_the_first() -> None:
    # A list built from the wrong queryset is wrong in several places at once,
    # and finding that out one round trip at a time is the slow way.
    with pytest.raises(InvalidShape, match=r"98, 99"):
        _resolve([], rows=2, fan_out=FanOut(Constant(1), parents=[98, 99]))


def test_a_rounded_uniform_can_size_a_partition() -> None:
    # Uniform(1, 10) is the spelling FanOut's own docstring recommends and the
    # one two refusals suggest, and places=0 is the natural way to say that a
    # fan-out size is a count -- so the failing form was one keyword away from
    # the form the package tells you to write.
    #
    # The guard was isinstance(weight, (int, float)) while the very next line
    # already did float(weight), so it was stricter than the arithmetic it
    # protects. Decimal is numbers.Number but deliberately *not* numbers.Real,
    # so widening to Real alone would not have reached it.
    plan = _resolve(list(range(10)), rows=100, fan_out=FanOut(Uniform(1, 10, places=0)))

    assert sum(plan.sizes()) == 100


def test_a_fraction_can_size_one_too() -> None:
    # The same widening, and the reason it is to a protocol rather than to a
    # list of two more types: a caller's own weights may be Fractions or numpy
    # scalars, and none of them is a declaration mistake.
    plan = _resolve(list(range(4)), rows=40, fan_out=FanOut(Constant(Fraction(3, 2))))

    assert sum(plan.sizes()) == 40


def test_a_size_that_is_not_a_number_is_still_refused_and_now_names_the_remedy() -> None:
    with pytest.raises(InvalidShape) as raised:
        _resolve(list(range(4)), rows=40, fan_out=FanOut(Constant("three")))

    message = str(raised.value)
    assert "'three'" in message
    # The one refusal in the package that named no remedy.
    assert "a count" in message


def test_a_boolean_size_is_refused_rather_than_counted_as_one() -> None:
    # bool is an int and is numbers.Real, so widening has to keep excluding it:
    # a distribution handing back True has almost certainly been written for a
    # different column.
    with pytest.raises(InvalidShape, match="True"):
        _resolve(list(range(4)), rows=40, fan_out=FanOut(Constant(True)))


def test_a_narrowed_partition_does_not_depend_on_what_the_keys_are() -> None:
    """The central promise, which ``parents=`` was breaking.

    Keys come back from the database ordered by primary key, and the weights
    were assigned by position in *that* order -- so with UUID parents, which is
    what a factory row on a modern schema has, the partition was a function of
    values nobody declared. Measured before the fix: one declaration gave the
    first-named parent 5, 11 or 79 rows across twelve runs, because the sort
    order moved. Two builds of one shape have to agree, and that is the reason
    ``UuidKeys`` derives rather than draws.
    """
    named = [900, 100, 500]
    # The database hands these back sorted; the declaration says otherwise.
    plan = resolve_paired_keys(named, sorted(named))

    assert plan == named


def test_reversing_the_named_parents_reverses_the_partition() -> None:
    # The symptom that was reported: reversing the list produced an identical
    # partition, because the list order reached nothing.
    forward = _resolve_named([10, 20, 30])
    backward = _resolve_named([30, 20, 10])

    assert forward != backward
    assert sorted(forward.values()) == sorted(backward.values())
    # The same key gets a different share, because it sits at a different
    # position in what the caller wrote.
    assert forward[10] == backward[30]


def _resolve_named(named: list[int]) -> dict[int, int]:
    plan = resolve_fan_out(
        FanOut(Zipf(1.2), parents=named),
        Company,
        60,
        seed=5,
        table="testapp_session",
        field="company",
        connection=_Connection(sorted(named)),
    )
    return dict(zip(plan.parent_keys(), plan.sizes(), strict=True))


def resolve_paired_keys(named: list[int], returned: list[int]) -> list[object]:
    plan = resolve_fan_out(
        FanOut(Constant(1), parents=named),
        Company,
        len(named),
        seed=1,
        table="t",
        field="company",
        connection=_Connection(returned),
    )
    return list(plan.parent_keys())


def test_a_cap_bounds_every_parent_and_the_partition_still_covers_the_rows() -> None:
    keys = list(range(1, 201))
    uncapped = _resolve(keys, rows=2000).sizes()
    capped = _resolve(keys, rows=2000, fan_out=FanOut(Zipf(1.2), max_children=20)).sizes()

    # Sized so the cap binds: without it the head is far past twenty, so a cap
    # that did nothing would fail the first assertion rather than pass it.
    assert max(uncapped) > 40
    assert max(capped) <= 20
    assert sum(capped) == 2000


def test_the_excess_goes_to_the_uncapped_parents_in_proportion() -> None:
    # Water-filling rather than clipping: the rows a whale cannot take are
    # re-spread over the rest by their weights, so the order of the tail is the
    # order the distribution gave it, with the head flattened onto the cap.
    keys = list(range(1, 101))
    uncapped = _resolve(keys, rows=1000).sizes()
    capped = _resolve(keys, rows=1000, fan_out=FanOut(Zipf(1.2), max_children=30)).sizes()

    below = [index for index, size in enumerate(capped) if size < 30]
    assert below
    assert all(capped[index] >= uncapped[index] for index in below)


def test_a_childless_parent_takes_none_of_the_excess() -> None:
    # Or childless= would stop meaning what it says: the rows a capped head
    # gives up go to parents with a weight, never to the ones declared empty.
    keys = list(range(1, 101))
    declared = FanOut(Zipf(1.2), childless=0.3)
    childless = {
        index
        for index, size in enumerate(_resolve(keys, rows=100_000, fan_out=declared).sizes())
        if size == 0
    }
    # Near the ceiling of what the cap allows, so the excess has to go far down
    # the tail to fit.
    parents_with_weight = len(keys) - len(childless)
    rows = parents_with_weight * 10 - 3
    capped = _resolve(
        keys, rows=rows, fan_out=FanOut(Zipf(1.2), childless=0.3, max_children=10)
    ).sizes()

    assert childless
    assert {index for index, size in enumerate(capped) if size == 0} == childless
    assert max(capped) <= 10
    assert sum(capped) == rows


def test_a_world_that_fills_every_parent_to_the_cap_is_built() -> None:
    # The boundary of the refusal below, held from this side: exactly as many
    # rows as the cap allows is a world, and every parent is full.
    capped = _resolve(list(range(1, 11)), rows=50, fan_out=FanOut(Zipf(1.2), max_children=5))

    assert capped.sizes() == [5] * 10


def test_rows_beyond_what_the_cap_allows_are_refused_with_the_arithmetic() -> None:
    with pytest.raises(InvalidShape) as raised:
        _resolve(list(range(1, 11)), rows=51, fan_out=FanOut(Zipf(1.2), max_children=5))

    message = str(raised.value)
    assert "max_children=5" in message
    assert "at most 50" in message
    assert "51" in message


def test_the_refusal_counts_only_parents_with_a_weight() -> None:
    # A childless parent can hold none of the rows, so the ceiling is the cap
    # times the parents that can, not times the parent table.
    keys = list(range(1, 101))
    declared = FanOut(Zipf(1.2), childless=0.3, max_children=10)
    with_weight = sum(
        1
        for size in _resolve(keys, rows=100_000, fan_out=FanOut(Zipf(1.2), childless=0.3)).sizes()
        if size
    )

    _resolve(keys, rows=with_weight * 10, fan_out=declared)
    with pytest.raises(InvalidShape, match=f"at most {with_weight * 10}"):
        _resolve(keys, rows=with_weight * 10 + 1, fan_out=declared)


def test_a_cap_that_never_binds_leaves_the_partition_as_it_was() -> None:
    # The uncapped arithmetic is the capped arithmetic with nothing capped, so
    # a cap above the largest share must not move a single row.
    keys = list(range(100, 300))
    uncapped = _resolve(keys, rows=5000).sizes()

    assert _resolve(keys, rows=5000, fan_out=FanOut(Zipf(1.2), max_children=5000)).sizes() == (
        uncapped
    )


def _busiest_share_cell(s: float, parents: int) -> str:
    rows = 1_000_000
    connection = _Connection(list(range(1, parents + 1)))
    shares = sorted(
        max(resolve_fan_out(FanOut(Zipf(s)), Company, rows, seed, "t", "f", connection).sizes())
        / rows
        for seed in range(20)
    )
    median = (shares[9] + shares[10]) / 2
    return f"{median:.1%} ({shares[1]:.1%}-{shares[-2]:.1%})".replace("%-", "-")


def test_the_relations_page_table_is_what_the_partition_draws() -> None:
    # The table of s against the busiest parent's share is measured, not
    # derived, so it is re-measured here: a change to the partition or to Zipf
    # that moved it would otherwise leave readers choosing an exponent from
    # numbers the code no longer produces.
    page = (Path(__file__).resolve().parents[2] / "docs" / "relations.md").read_text()
    rows = [line for line in page.splitlines() if line.startswith(("| 1.", "| 2.", "| 3."))]
    assert len(rows) == 6

    for line in rows:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        s = float(cells[0])
        assert cells[1:] == [_busiest_share_cell(s, parents) for parents in (100, 1_000, 10_000)], (
            line
        )
