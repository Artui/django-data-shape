"""Turning a declared tree into one parent per row, every parent an earlier row."""

from __future__ import annotations

import math
import numbers
from array import array
from bisect import bisect_right
from decimal import Decimal
from fractions import Fraction
from itertools import accumulate, repeat

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.relations.tree import Tree
from django_data_shape.relations.tree_plan import TreePlan
from django_data_shape.utils import draw, field_stream


def resolve_tree(tree: Tree, rows: int, seed: int, table: str, field: str) -> TreePlan:
    """Give every non-root row a parent at a lower index, by the declared weights.

    No query, unlike a fan-out: the parents are rows of this same table, whose
    keys are a function of their index, so the whole tree is arithmetic.

    **Three steps, and the second is the one that keeps the whales scattered.**

    1. The roots: ``ceil(rows * roots)`` of them, evenly spaced from row 0.
    2. Every row's target number of children, its weight's share of the
       non-root rows by largest remainder -- the same partition a ``FanOut``
       makes, with the weight keyed on the row's position, so a heavy row is
       wherever its draw put it.
    3. Each non-root row, in index order, takes as its parent an earlier row
       that still has room, chosen with probability proportional to the room
       left. A heavy row's room lasts, so its children spread far through the
       rows after it; a row with room for one child usually gives it to a row
       soon after.

    Step 3 cannot always honour step 2, and the rule is why. A row has only the
    rows after it to be its children, so a heavy row near the end of the table
    is left short, and a row that finds no earlier row with room left -- early
    on, before enough targets have accumulated -- falls back to an earlier row
    chosen by weight alone, which places a child past that row's target.
    Measured on ``Zipf()`` at 200,000 rows that is about 2% of the children;
    ``docs/relations.md`` has the shape of it.
    """
    # Through the share's decimal reading rather than the float product, which
    # overshoots: 100 * 0.07 is 7.000000000000001, and its ceiling is 8.
    root_count = math.ceil(Fraction(repr(tree.roots)) * rows)
    children = rows - root_count
    # Nothing to place means nothing to weigh, as in a fan-out: a table of
    # roots alone is a legitimate thing to be, and refusing its weights would
    # refuse it.
    weights = _weights(tree, rows, seed, table, field) if children else []
    targets = _targets(tree, weights, children, table, field) if children else [0] * rows
    reach = array("d", accumulate(weights))
    pick_stream = field_stream(seed, table, f"{field}:pick")

    parents = array("q", bytes(8 * rows))
    # One entry per unit of room an earlier row still has, so a uniform pick
    # from it is a pick weighted by room left. Taken by swapping the last entry
    # into the hole, which keeps every step O(1) where a Fenwick tree over the
    # rows gives the same draw at O(log n).
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
                # Nobody earlier has room left, so the child goes where the
                # weights alone would send it. Clamped to the row before this
                # one, which is also where a run of zero weights sends it:
                # bisect then passes every earlier row, none having reached
                # the threshold, and the nearest earlier row is still earlier.
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
        # The numeric tower plus Decimal, and never a bool: the reasoning is
        # the fan-out's, in resolve_fan_out._sizes.
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
    # sum, not fsum: fsum raises on a sum past a float rather than returning
    # the infinity the refusal below reads.
    total = sum(weights)
    if not math.isfinite(total) or total <= 0:
        raise InvalidShape(
            f"{table}.{field} draws tree sizes from {tree.sizes!r} that sum to {total!r} over "
            f"{len(weights)} rows, so there is no share of the {children} children to give any of them. "
            "The sizes are normalised: declare a distribution with some weight, and with numbers "
            "small enough to add up."
        )
    exact = [weight / total * children for weight in weights]
    targets = [int(value) for value in exact]
    # The leftover rows go to the largest fractional parts, as in a fan-out.
    order = sorted(
        range(len(weights)), key=lambda index: exact[index] - targets[index], reverse=True
    )
    for index in order[: children - sum(targets)]:
        targets[index] += 1
    return targets
