"""Whether a tree gives any row more than one child."""

from __future__ import annotations

from django_data_shape.distributions.bounded import Bounded
from django_data_shape.relations.count_roots import count_roots
from django_data_shape.relations.tree import Tree


def gives_no_row_two_children(tree: Tree, rows: int) -> bool:
    """A proof rather than a probability, from the declaration alone. Two ways to know it.

    **At most one row has a parent**, by arithmetic on the roots, read the way
    the build reads them. One child has no sibling, whatever the sizes weigh.

    **The sizes hold one value.** ``_targets`` then gives every row the same
    share, below one, and its largest remainder gives one child each to the first
    rows, as many rows as there are children. Each of those units is taken once.
    The fall-back that goes past a target never runs: row 0 is always a root, so
    up to the last row with a target there is always an earlier row with room, and
    after it the units left equal the children still to come. So the seed and the
    table's name change nothing here, unlike every other figure about a tree.
    ``Constant(0)`` weighs nothing and is refused when the tree is resolved.

    Two values are not enough, since one row can then weigh more than a share of
    one.

    It answers for the tree and not for a constraint over it: whether a root's NULL
    is its own group is the constraint's to say, so ``Table``, which reads
    ``unique=True`` and its one meaning, calls this directly, and
    ``check_constraints`` asks the NULL question first.

    .. warning::

       **Two conjuncts the coverage gate cannot see.** The ``children`` bound is
       held from both sides by
       ``test_a_tree_with_one_child_has_no_siblings_to_collide`` and
       ``test_a_tree_with_two_children_can_give_them_one_parent``, and the value
       count by ``test_a_tree_of_two_sizes_is_not_flat``.
    """
    children = rows - count_roots(tree, rows)
    if children <= 1:
        return True
    return isinstance(tree.sizes, Bounded) and tree.sizes.distinct_values() == 1
