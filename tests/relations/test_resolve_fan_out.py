from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from django_data_shape import Constant, FanOut, InvalidShape, Skew, Uniform, Zipf
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
    """Everything resolve_fan_out touches, and nothing else."""

    def __init__(self, keys: list[int]) -> None:
        self._keys = keys
        self.last_sql = ""
        self.last_params: Any = None
        self.ops = type(
            "Ops",
            (),
            {
                "quote_name": staticmethod(lambda name: f'"{name}"'),
                # Both reach the key field's get_db_prep_value, which is how a named key is sent.
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

    # The partition sums to the declared count exactly, with no rounding loss.
    assert sum(plan.sizes()) == 1000


def test_keys_come_from_the_parent_rather_than_being_assumed() -> None:
    # The correction that prompted this design.
    keys = [7, 41, 9001, 12345]
    plan = _resolve(keys, rows=200)

    emitted = {plan.key_for(row) for row in range(200)}
    assert emitted <= set(keys)
    assert min(emitted) >= 7


def test_a_zipf_fan_out_gives_a_head_and_a_tail() -> None:
    sizes = sorted(_resolve(list(range(1, 201)), rows=20_000).sizes(), reverse=True)

    # A head and a tail is what production has.
    assert sizes[0] > 10 * sizes[len(sizes) // 2]


def test_the_childless_tail_is_representable() -> None:
    plan = _resolve(list(range(1, 101)), rows=5000, fan_out=FanOut(Zipf(), childless=0.4))
    sizes = plan.sizes()

    # The childless tail that hand-written fixtures always omit.
    assert 25 <= sizes.count(0) <= 55
    assert sum(sizes) == 5000


def test_a_null_share_thins_the_children() -> None:
    plan = _resolve(list(range(1, 51)), rows=4000, fan_out=FanOut(Zipf(), null=0.25))
    keys = [plan.key_for(row) for row in range(4000)]
    nulls = sum(1 for key in keys if key is None)

    # null_frac is planner-visible, so a never-null optional foreign key is unrealistic.
    assert 850 < nulls < 1150


def test_arrival_and_grouped_place_the_same_children_differently() -> None:
    keys = list(range(1, 21))
    grouped = _resolve(keys, rows=400, fan_out=FanOut(Uniform(1, 2), placement="grouped"))
    arrival = _resolve(keys, rows=400, fan_out=FanOut(Uniform(1, 2), placement="arrival"))

    # Same partition, different emission order.
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
    # Nothing is buffered or sorted, and one row's answer depends on no other row.
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
    # Rows of one group are scattered on purpose, yet every position gets exactly one row.
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
    # bisect_right steps past empty ranges, so a childless parent is unreachable by design.
    plan = _resolve(list(range(100, 140)), rows=200, fan_out=FanOut(Zipf(1.4), childless=0.5))

    assert 0 in plan.sizes()
    assert all(plan.group_position(row)[1] > 0 for row in range(200))


def test_parents_narrows_which_keys_the_partition_covers() -> None:
    # What is checked here is that the partition covers exactly those.
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
    # A string key from JSON must be read as the key's type before it is compared.
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
    # A key from another database would silently send every child somewhere else.
    with pytest.raises(InvalidShape) as raised:
        _resolve([20], rows=2, fan_out=FanOut(Constant(1), parents=[20, 99]))

    message = str(raised.value)
    assert "99" in message
    assert "Company" in message


def test_the_refusal_names_every_missing_key_and_not_only_the_first() -> None:
    # A list built from the wrong queryset is wrong in several places at once.
    with pytest.raises(InvalidShape, match=r"98, 99"):
        _resolve([], rows=2, fan_out=FanOut(Constant(1), parents=[98, 99]))


def test_a_rounded_uniform_can_size_a_partition() -> None:
    # places=0 is the natural spelling of a count, so the failing form is one keyword away.
    plan = _resolve(list(range(10)), rows=100, fan_out=FanOut(Uniform(1, 10, places=0)))

    assert sum(plan.sizes()) == 100


def test_a_fraction_can_size_one_too() -> None:
    # Caller weights may be Fractions or numpy scalars, and none of them is a mistake.
    plan = _resolve(list(range(4)), rows=40, fan_out=FanOut(Constant(Fraction(3, 2))))

    assert sum(plan.sizes()) == 40


def test_a_size_that_is_not_a_number_is_still_refused_and_now_names_the_remedy() -> None:
    with pytest.raises(InvalidShape) as raised:
        _resolve(list(range(4)), rows=40, fan_out=FanOut(Constant("three")))

    message = str(raised.value)
    assert "'three'" in message
    # The one refusal in the package that named no remedy.
    assert "a count" in message


@pytest.mark.parametrize(
    "sizes",
    [Skew({-1: 3, 5: 1}), Constant(-1), Constant(float("inf")), Constant(float("nan"))],
)
@pytest.mark.parametrize("max_children", [None, 3])
def test_a_negative_or_infinite_size_is_refused_and_names_the_field(
    sizes: Any, max_children: int | None
) -> None:
    # Uncapped, a negative weight gave sizes that did not add up to the rows.
    with pytest.raises(InvalidShape, match="zero or more, finite") as raised:
        _resolve(list(range(1, 11)), rows=5, fan_out=FanOut(sizes, max_children=max_children))

    assert "testapp_session.company" in str(raised.value)


def test_sizes_whose_sum_overflows_are_refused() -> None:
    # Each weight is finite but their sum is not, so every share would be zero.
    with pytest.raises(InvalidShape, match="too large to divide by"):
        _resolve(list(range(1, 31)), rows=100, fan_out=FanOut(Constant(1e308)))


def test_a_boolean_size_is_refused_rather_than_counted_as_one() -> None:
    # bool is an int and a Real, so widening must keep excluding it.
    with pytest.raises(InvalidShape, match="True"):
        _resolve(list(range(4)), rows=40, fan_out=FanOut(Constant(True)))


def test_a_narrowed_partition_does_not_depend_on_what_the_keys_are() -> None:
    """The central promise, which ``parents=`` was breaking."""
    named = [900, 100, 500]
    plan = resolve_paired_keys(named, sorted(named))

    assert plan == named


def test_reversing_the_named_parents_reverses_the_partition() -> None:
    # Reversing the list gave an identical partition, because list order reached nothing.
    forward = _resolve_named([10, 20, 30])
    backward = _resolve_named([30, 20, 10])

    assert forward != backward
    assert sorted(forward.values()) == sorted(backward.values())
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

    # Sized so the cap binds: a cap that did nothing would fail the first assertion.
    assert max(uncapped) > 40
    assert max(capped) <= 20
    assert sum(capped) == 2000


def test_the_excess_goes_to_the_uncapped_parents_in_proportion() -> None:
    # Water-filling: the rows a capped parent cannot take are re-spread by their weights.
    keys = list(range(1, 101))
    uncapped = _resolve(keys, rows=1000).sizes()
    capped = _resolve(keys, rows=1000, fan_out=FanOut(Zipf(1.2), max_children=30)).sizes()

    below = [index for index, size in enumerate(capped) if size < 30]
    assert below
    assert all(capped[index] >= uncapped[index] for index in below)


def test_a_childless_parent_takes_none_of_the_excess() -> None:
    # Near the ceiling of what the cap allows, so the excess has to go far down the tail to fit.
    keys = list(range(1, 101))
    declared = FanOut(Zipf(1.2), childless=0.3)
    childless = {
        index
        for index, size in enumerate(_resolve(keys, rows=100_000, fan_out=declared).sizes())
        if size == 0
    }
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
    # Exactly as many rows as the cap allows is a world, and every parent is full.
    capped = _resolve(list(range(1, 11)), rows=50, fan_out=FanOut(Zipf(1.2), max_children=5))

    assert capped.sizes() == [5] * 10


def test_rows_beyond_what_the_cap_allows_are_refused_with_the_arithmetic() -> None:
    with pytest.raises(InvalidShape) as raised:
        _resolve(list(range(1, 11)), rows=51, fan_out=FanOut(Zipf(1.2), max_children=5))

    message = str(raised.value)
    assert "max_children=5" in message
    assert "at most 50" in message
    assert "51" in message


def test_the_refusal_offers_childless_only_to_a_declaration_that_has_one() -> None:
    # Lowering a childless share of zero frees nothing, so a declaration without one is not told to.
    with pytest.raises(InvalidShape) as plain:
        _resolve(list(range(1, 11)), rows=51, fan_out=FanOut(Zipf(1.2), max_children=5))
    with pytest.raises(InvalidShape) as with_childless:
        _resolve(
            list(range(1, 101)),
            rows=1001,
            fan_out=FanOut(Zipf(1.2), childless=0.3, max_children=10),
        )

    assert "childless" not in str(plain.value)
    assert str(plain.value).endswith("Raise max_children or declare fewer rows.")
    assert str(with_childless.value).endswith(
        "Raise max_children, lower childless, or declare fewer rows."
    )


def test_the_refusal_counts_only_parents_with_a_weight() -> None:
    # A childless parent can hold no rows, so the ceiling counts only parents that can.
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


@pytest.mark.parametrize(
    ("weights", "rows"),
    [
        ({1.3: 0.5, 9.1e13: 0.5}, 18299),
        ({1: 0.5, 10**17: 0.5}, 18946),
    ],
)
def test_weights_far_apart_still_partition_within_the_cap(
    weights: dict[float, float], rows: int
) -> None:
    # The weight left after the heavy parents are capped has to keep the light ones.
    sizes = _resolve(
        list(range(1, 31)), rows=rows, fan_out=FanOut(Skew(weights), max_children=1000)
    ).sizes()

    assert sum(sizes) == rows
    assert min(sizes) >= 0
    assert max(sizes) <= 1000


def test_a_cap_that_never_binds_leaves_the_partition_as_it_was() -> None:
    # A cap above the largest share must not move a single row.
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
    # The table of exponents is measured, so a change must be re-measured here.
    page = (Path(__file__).resolve().parents[2] / "docs" / "relations.md").read_text()
    rows = [line for line in page.splitlines() if line.startswith(("| 1.", "| 2.", "| 3."))]
    assert len(rows) == 6

    for line in rows:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        s = float(cells[0])
        assert cells[1:] == [_busiest_share_cell(s, parents) for parents in (100, 1_000, 10_000)], (
            line
        )
