from __future__ import annotations

import math
from collections import Counter
from decimal import Decimal
from fractions import Fraction
from itertools import pairwise

import pytest

from django_data_shape import Constant, InvalidShape, KeyFunction, Table, Tree, Uniform, Zipf
from django_data_shape.generation.generate_rows import generate_rows
from django_data_shape.relations.resolve_tree import resolve_tree
from django_data_shape.relations.tree_plan import TreePlan
from tests.testapp.models import Category


class _Sizes:
    """A size distribution handing back whatever the test needs, by row."""

    def __init__(self, value: object, *, below: int = 0, then: object = 1.0) -> None:
        self._value = value
        self._below = below
        self._then = then

    def value(self, row: int, draw: float) -> object:
        return self._value if row < self._below or not self._below else self._then

    def canonical(self) -> object:
        return (repr(self._value),)


def _parents(plan: TreePlan, rows: int) -> list[int | None]:
    return [plan.parent_for(row) for row in range(rows)]


def _resolve(tree: Tree, rows: int, seed: int = 0) -> list[int | None]:
    return _parents(resolve_tree(tree, rows, seed, "testapp_category", "parent"), rows)


def test_every_parent_is_an_earlier_row() -> None:
    parents = _resolve(Tree(roots=0.05, sizes=Zipf()), 3_000)
    assert all(parent < row for row, parent in enumerate(parents) if parent is not None)


@pytest.mark.parametrize(
    ("rows", "roots", "expected"),
    # 100 * 0.07 is 7.000000000000001 as floats, whose ceiling is 8.
    [(1, 0.5, 1), (7, 0.3, 3), (1_000, 0.05, 50), (999, 0.333, 333), (100, 0.07, 7)],
)
def test_the_root_count_is_exact_and_starts_at_the_first_row(
    rows: int, roots: float, expected: int
) -> None:
    parents = _resolve(Tree(roots=roots, sizes=Zipf()), rows)
    placed = [row for row, parent in enumerate(parents) if parent is None]
    assert len(placed) == expected
    assert placed[0] == 0


def test_the_roots_are_spread_through_the_table_rather_than_gathered() -> None:
    parents = _resolve(Tree(roots=0.1, sizes=Zipf()), 1_000)
    placed = [row for row, parent in enumerate(parents) if parent is None]
    assert {later - earlier for earlier, later in pairwise(placed)} == {10}


def test_an_empty_table_resolves_to_no_rows() -> None:
    assert _resolve(Tree(roots=0.5, sizes=Zipf()), 0) == []


def test_a_table_of_roots_alone_weighs_nothing() -> None:
    # One row is one root and no child, so no share is ever divided.
    assert _resolve(Tree(roots=0.5, sizes=_Sizes("not a number")), 1) == [None]


def test_two_resolutions_of_one_declaration_agree() -> None:
    tree = Tree(roots=0.05, sizes=Zipf())
    assert _resolve(tree, 2_000, seed=4) == _resolve(tree, 2_000, seed=4)
    assert _resolve(tree, 2_000, seed=4) != _resolve(tree, 2_000, seed=5)


def test_the_families_follow_the_weights_rather_than_flattening_them() -> None:
    # A construction that ignored the weights would make the two look alike.
    def largest(sizes: object) -> int:
        parents = _resolve(Tree(roots=0.05, sizes=sizes), 5_000)
        return max(Counter(parent for parent in parents if parent is not None).values())

    assert largest(Uniform(1, 2)) < 10
    assert largest(Zipf()) > 100


def test_a_child_with_no_earlier_room_goes_to_an_earlier_row_by_weight() -> None:
    # With every earlier weight zero, the nearest earlier row is the one a child gets.
    parents = _resolve(Tree(roots=0.01, sizes=_Sizes(0.0, below=10)), 100)
    assert parents[:10] == [None, 0, 1, 2, 3, 4, 5, 6, 7, 8]


@pytest.mark.parametrize("weight", [Decimal("2"), Fraction(3, 2), 4])
def test_any_real_number_is_a_weight(weight: object) -> None:
    parents = _resolve(Tree(roots=0.1, sizes=_Sizes(weight)), 50)
    assert sum(parent is None for parent in parents) == 5


@pytest.mark.parametrize("weight", ["3", True, None])
def test_a_size_that_is_not_a_number_is_refused(weight: object) -> None:
    with pytest.raises(InvalidShape, match="needs numeric tree sizes"):
        _resolve(Tree(roots=0.1, sizes=_Sizes(weight)), 50)


@pytest.mark.parametrize("weight", [-1.0, math.inf, math.nan])
def test_a_negative_or_infinite_size_is_refused(weight: float) -> None:
    with pytest.raises(InvalidShape, match="tree sizes of zero or more, finite"):
        _resolve(Tree(roots=0.1, sizes=_Sizes(weight)), 50)


@pytest.mark.parametrize("weight", [0.0, 1e308])
def test_sizes_that_sum_to_nothing_or_past_a_float_are_refused(weight: float) -> None:
    with pytest.raises(InvalidShape, match="no share of the 45 children"):
        _resolve(Tree(roots=0.1, sizes=_Sizes(weight)), 50)


def test_a_parent_key_is_the_key_its_own_strategy_gives_that_row() -> None:
    # The parent key comes from the table's own strategy, not row + 1.
    table = Table(
        Category,
        rows=200,
        keys=KeyFunction(lambda row: 1_000 + row),
        name=Constant("c"),
        parent=Tree(roots=0.05, sizes=Zipf()),
    )
    rows = list(generate_rows(table, seed=2))
    keys = {row[0] for row in rows}
    slot = [name for name, _field in table.columns()].index("parent") + 1
    parents = [row[slot] for row in rows]
    assert sum(parent is None for parent in parents) == 10
    assert {parent for parent in parents if parent is not None} <= keys
    assert all(
        parent < row[0] for row, parent in zip(rows, parents, strict=True) if parent is not None
    )


def _shape_of(sizes: object, rows: int) -> tuple[int, int]:
    """The median rows from parent to child, and the deepest row."""
    parents = _resolve(Tree(roots=0.05, sizes=sizes), rows)
    depth = [0] * rows
    gaps = []
    for row, parent in enumerate(parents):
        if parent is not None:
            depth[row] = depth[parent] + 1
            gaps.append(row - parent)
    return sorted(gaps)[len(gaps) // 2], max(depth)


def test_heavy_sizes_spread_a_family_far_and_keep_the_tree_shallow() -> None:
    # The first two rows of the table in docs/relations.md, at 20,000 rows.
    gap, deepest = _shape_of(Zipf(), 20_000)
    assert gap > 200
    assert deepest < 50


def test_flat_sizes_make_chains_of_near_neighbours() -> None:
    gap, deepest = _shape_of(Uniform(1, 3), 20_000)
    assert gap < 50
    assert deepest > 100
