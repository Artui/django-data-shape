from __future__ import annotations

import math
import numbers
from array import array
from bisect import bisect_right
from decimal import Decimal
from itertools import accumulate, repeat

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.relations.count_roots import count_roots
from django_data_shape.relations.tree import Tree
from django_data_shape.relations.tree_plan import TreePlan
from django_data_shape.utils import draw, field_stream


def resolve_tree(tree: Tree, rows: int, seed: int, table: str, field: str) -> TreePlan:
    """Give every non-root row a parent at a lower index, by the declared weights."""
    root_count = count_roots(tree, rows)
    children = rows - root_count
    weights = _weights(tree, rows, seed, table, field) if children else []
    targets = _targets(tree, weights, children, table, field) if children else [0] * rows
    reach = array("d", accumulate(weights))
    pick_stream = field_stream(seed, table, f"{field}:pick")

    parents = array("q", bytes(8 * rows))
    room = array("q")
    next_root, placed_roots = 0, 0
    for row in range(rows):
        if row == next_root:
            parents[row] = -1
            placed_roots += 1
            next_root = placed_roots * rows // root_count if placed_roots < root_count else rows
        else:
            share = draw(pick_stream, row)
            if room:
                slot = int(share * len(room))
                parent = room[slot]
                room[slot] = room[-1]
                room.pop()
            else:
                parent = min(bisect_right(reach, share * reach[row - 1], 0, row), row - 1)
            parents[row] = parent
        if targets[row]:
            room.extend(repeat(row, targets[row]))
    return TreePlan(parents)


def _weights(tree: Tree, rows: int, seed: int, table: str, field: str) -> list[float]:
    """Every row's weight, checked the way a fan-out's are."""
    stream = field_stream(seed, table, f"{field}:weight")
    weights: list[float] = []
    for row in range(rows):
        weight = tree.sizes.value(row, draw(stream, row))
        if isinstance(weight, bool) or not isinstance(weight, (numbers.Real, Decimal)):
            raise InvalidShape(
                f"{table}.{field} needs numeric tree sizes, but {tree.sizes!r} produced "
                f"{weight!r}. A tree size weighs how many children a row gets, so the "
                "distribution has to produce numbers; they are normalised, so their scale does "
                "not matter."
            )
        value = float(weight)
        if value < 0 or not math.isfinite(value):
            raise InvalidShape(
                f"{table}.{field} needs tree sizes of zero or more, finite, but "
                f"{tree.sizes!r} produced {weight!r}. A size weighs how many children a row "
                "gets, so it cannot be negative or infinite."
            )
        weights.append(value)
    return weights


def _targets(tree: Tree, weights: list[float], children: int, table: str, field: str) -> list[int]:
    """How many children each row is meant to have, summing to exactly ``children``."""
    total = sum(weights)
    if not math.isfinite(total) or total <= 0:
        raise InvalidShape(
            f"{table}.{field} draws tree sizes from {tree.sizes!r} that sum to {total!r} over "
            f"{len(weights)} rows, so there is no share of the {children} children to give any of "
            "them. "
            "The sizes are normalised: declare a distribution with some weight, and with numbers "
            "small enough to add up."
        )
    exact = [weight / total * children for weight in weights]
    targets = [int(value) for value in exact]
    order = sorted(
        range(len(weights)), key=lambda index: exact[index] - targets[index], reverse=True
    )
    for index in order[: children - sum(targets)]:
        targets[index] += 1
    return targets
