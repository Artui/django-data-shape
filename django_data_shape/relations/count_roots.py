"""How many rows of a tree have no parent."""

from __future__ import annotations

import math
from fractions import Fraction

from django_data_shape.relations.tree import Tree


def count_roots(tree: Tree, rows: int) -> int:
    """The exact number of roots, which a refusal and the build have to agree on.

    Through the share's decimal reading rather than the float product, which
    overshoots: 100 * 0.07 is 7.000000000000001, and its ceiling is 8.
    """
    return math.ceil(Fraction(repr(tree.roots)) * rows)
