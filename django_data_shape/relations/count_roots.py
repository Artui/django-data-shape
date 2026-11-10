from __future__ import annotations

import math
from fractions import Fraction

from django_data_shape.relations.tree import Tree


def count_roots(tree: Tree, rows: int) -> int:
    """The exact number of roots, which a refusal and the build have to agree on."""
    return math.ceil(Fraction(repr(tree.roots)) * rows)
