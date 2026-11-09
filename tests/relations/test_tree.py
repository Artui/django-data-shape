"""Declaring a tree, and the declarations a tree refuses."""

from __future__ import annotations

import pytest

from django_data_shape import (
    Constant,
    Copied,
    FanOut,
    InvalidShape,
    PerParent,
    Shape,
    Skew,
    Table,
    Tree,
    Zipf,
)
from django_data_shape.databases.shape_digest import shape_digest
from django_data_shape.distributions.distribution import Distribution
from tests.testapp.models import Category, RootlessNode, Session, Successor


@pytest.mark.parametrize("roots", [0, 0.0, 1, 1.0, -0.1, 1.5, True, "0.05"])
def test_a_root_share_outside_the_open_unit_interval_is_refused(roots: object) -> None:
    with pytest.raises(InvalidShape, match="Tree roots is the share of rows with no parent"):
        Tree(roots=roots, sizes=Zipf())


def test_a_tree_reads_back_what_it_was_given() -> None:
    sizes = Zipf(1.5)
    tree = Tree(roots=0.25, sizes=sizes)
    assert tree.roots == 0.25
    assert tree.sizes is sizes
    assert tree.canonical() == (0.25, sizes)
    assert repr(tree) == "Tree(roots=0.25, sizes=Zipf(1.5))"


def test_the_root_share_is_part_of_the_cached_world() -> None:
    def digest(roots: float) -> str:
        return shape_digest(
            Shape(
                Table(Category, rows=10, name=Constant("c"), parent=Tree(roots=roots, sizes=Zipf()))
            )
        )

    assert digest(0.05) == digest(0.05)
    assert digest(0.05) != digest(0.1)


def test_a_tree_on_a_nullable_key_to_its_own_table_is_accepted() -> None:
    table = Table(Category, rows=10, name=Constant("c"), parent=Tree(roots=0.1, sizes=Zipf()))
    assert isinstance(table.fields["parent"], Tree)


def test_a_tree_on_a_key_to_another_table_is_refused() -> None:
    with pytest.raises(InvalidShape, match="points at Company, not at Session") as refused:
        Table(Session, rows=5, label=Constant("x"), company=Tree(roots=0.1, sizes=Zipf()))
    assert "company=FanOut(Zipf())" in str(refused.value)


def test_a_tree_on_a_column_that_refuses_null_is_refused() -> None:
    with pytest.raises(InvalidShape, match="RootlessNode.parent cannot be null"):
        Table(RootlessNode, rows=5, parent=Tree(roots=0.1, sizes=Zipf()))


def test_a_tree_on_a_unique_self_reference_is_refused() -> None:
    with pytest.raises(InvalidShape, match="Successor.previous is unique") as refused:
        Table(Successor, rows=5, previous=Tree(roots=0.1, sizes=Zipf()))
    # The generic unique-column check opens with the same prefix, so the
    # sentence only the Tree branch writes is what shows that branch ran.
    assert "previous=Tree(roots=0.1, sizes=Constant(1))" in str(refused.value)
    assert "A chain of one-to-one links" not in str(refused.value)


@pytest.mark.parametrize(
    "sizes", [Constant(1), Constant(3), Skew({3: 1})], ids=["one", "three", "skew_of_one"]
)
def test_a_tree_of_one_size_on_a_unique_self_reference_is_accepted(sizes: Distribution) -> None:
    # Table alone decides unique=True, and it answers past _check_trees too: the
    # generic unique-column check reads a tree as a draw if it is not told.
    assert isinstance(Table(Successor, rows=50, previous=Tree(roots=0.1, sizes=sizes)), Table)


def test_a_tree_with_one_child_on_a_unique_self_reference_is_accepted_whatever_it_weighs() -> None:
    # One child has no sibling, so the sizes decide nothing here.
    Table(Successor, rows=2, previous=Tree(roots=0.5, sizes=Zipf()))


def test_a_tree_with_two_children_on_a_unique_self_reference_is_refused() -> None:
    with pytest.raises(InvalidShape, match="Successor.previous is unique"):
        Table(Successor, rows=3, previous=Tree(roots=0.1, sizes=Zipf()))


def test_a_tree_of_two_sizes_on_a_unique_self_reference_is_refused() -> None:
    with pytest.raises(InvalidShape, match="Successor.previous is unique"):
        Table(Successor, rows=50, previous=Tree(roots=0.1, sizes=Skew({1: 1, 2: 1})))


def test_a_tree_on_a_column_that_is_not_a_relation_is_refused() -> None:
    with pytest.raises(InvalidShape, match="Category.name is not a relation"):
        Table(Category, rows=5, name=Tree(roots=0.1, sizes=Zipf()))


def test_a_self_referential_fan_out_names_the_tree_that_fills_it() -> None:
    with pytest.raises(InvalidShape, match="points at its own table") as refused:
        Table(Category, rows=5, name=Constant("c"), parent=FanOut(Zipf()))
    assert "parent=Tree(roots=0.05, sizes=Zipf())" in str(refused.value)


def test_a_value_on_a_self_reference_names_the_tree_among_the_remedies() -> None:
    with pytest.raises(InvalidShape, match="is a relation") as refused:
        Table(Category, rows=5, name=Constant("c"), parent=Constant(1))
    assert "a Tree" in str(refused.value)


def test_nothing_reads_a_parent_column_across_a_tree() -> None:
    with pytest.raises(InvalidShape, match="across parent, which is a Tree"):
        Table(
            Category,
            rows=5,
            name=Copied("parent.name"),
            parent=Tree(roots=0.1, sizes=Zipf()),
        )


def test_nothing_is_decided_per_family_of_a_tree() -> None:
    with pytest.raises(InvalidShape, match="per group of 'parent', which is a Tree"):
        Table(
            Category,
            rows=5,
            name=PerParent("parent", last="newest", rest="older"),
            parent=Tree(roots=0.1, sizes=Zipf()),
        )
