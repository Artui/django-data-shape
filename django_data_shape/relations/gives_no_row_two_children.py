from __future__ import annotations

from django_data_shape.distributions.bounded import Bounded
from django_data_shape.relations.count_roots import count_roots
from django_data_shape.relations.tree import Tree


def gives_no_row_two_children(tree: Tree, rows: int) -> bool:
    """Whether a tree gives every row at most one child, proved from the declaration alone."""
    children = rows - count_roots(tree, rows)
    if children <= 1:
        return True
    return isinstance(tree.sizes, Bounded) and tree.sizes.distinct_values() == 1
