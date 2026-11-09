"""A tree under a uniqueness that nothing arranges, refused when the shape is declared.

The models are declared here under ``isolate_apps`` rather than in the test app,
because every test below stops at ``Shape()`` or at the plan and none needs a
table. It also keeps this module importable against a tree without the refusals, where each
refusal test fails on its assertion rather than dying at import.
"""

from __future__ import annotations

import django
import pytest
from django.db import models
from django.test.utils import isolate_apps

from django_data_shape import (
    Constant,
    Derived,
    FanOut,
    InvalidShape,
    Paired,
    PerParent,
    Sequential,
    Shape,
    Skew,
    Table,
    Tree,
    Zipf,
)
from django_data_shape.relations.resolve_tree import resolve_tree
from tests.testapp.models import (
    Chain,
    Company,
    Folder,
    Person,
    Relay,
    RootlessLink,
    Successor,
    TiedLink,
)

with isolate_apps("tests.testapp"):

    class Branch(models.Model):
        """A self-key and a company under one uniqueness."""

        parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
        company = models.ForeignKey(Company, null=True, on_delete=models.CASCADE, related_name="+")

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["parent", "company"], name="one_branch_per_pair"),
            ]

    class Unit(models.Model):
        """The same pair with a third column, which is what can keep it apart."""

        parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
        company = models.ForeignKey(Company, null=True, on_delete=models.CASCADE, related_name="+")
        person = models.ForeignKey(Person, null=True, on_delete=models.CASCADE, related_name="+")
        label = models.CharField(max_length=50, null=True)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["parent", "company", "label"], name="one_unit_per_triple"
                ),
            ]

    class Edge(models.Model):
        """Two self-keys under one uniqueness."""

        left = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
        right = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["left", "right"], name="one_edge_per_pair"),
            ]

    class LabelledEdge(models.Model):
        """Two self-keys and a third column."""

        left = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
        right = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
        label = models.CharField(max_length=50, null=True)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["left", "right", "label"], name="one_edge_per_triple"
                ),
            ]

    class Reply(models.Model):
        """One accepted reply among the replies to a post."""

        parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
        company = models.ForeignKey(Company, null=True, on_delete=models.CASCADE, related_name="+")
        note = models.CharField(max_length=50, null=True)
        status = models.CharField(max_length=50, null=True)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["parent"],
                    condition=models.Q(status="accepted"),
                    name="one_accepted_reply",
                ),
            ]

    class Tagged(models.Model):
        """A conditional uniqueness over a tree and a second column."""

        parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
        tag = models.CharField(max_length=50, null=True)
        status = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["parent", "tag"],
                    condition=models.Q(status="live"),
                    name="one_live_tag",
                ),
            ]

    class Crossed(models.Model):
        """A conditional uniqueness over two self-keys."""

        left = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
        right = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
        status = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["left", "right"],
                    condition=models.Q(status="live"),
                    name="one_live_edge",
                ),
            ]

    class Grouped(models.Model):
        """A conditional uniqueness whose tree comes first and whose fan-out follows."""

        parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
        company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="+")
        status = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["parent", "company"],
                    condition=models.Q(status="live"),
                    name="one_live_per_pair",
                ),
            ]

    # The keyword arrived in Django 5.0, and the tests reading it skip below that.
    _NULLS_TOGETHER: dict[str, bool] = {"nulls_distinct": False} if django.VERSION >= (5, 0) else {}

    class NullFolder(models.Model):
        """Names unique among siblings, with every root counted as one sibling group."""

        parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
        name = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["parent", "name"], name="one_null_folder", **_NULLS_TOGETHER
                ),
            ]

    class NullBranch(models.Model):
        """A self-key and a company, with the roots' NULLs counted as one value."""

        parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
        company = models.ForeignKey(Company, null=True, on_delete=models.CASCADE, related_name="+")

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["parent", "company"], name="one_null_branch", **_NULLS_TOGETHER
                ),
            ]

    class NullReply(models.Model):
        """One accepted reply per parent, with the roots counted as one parent."""

        parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
        company = models.ForeignKey(Company, null=True, on_delete=models.CASCADE, related_name="+")
        status = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["parent"],
                    condition=models.Q(status="accepted"),
                    name="one_null_reply",
                    **_NULLS_TOGETHER,
                ),
            ]

    class NullGrouped(models.Model):
        """A conditional uniqueness over a tree and a fan-out, NULLs counted as one value."""

        parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
        company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="+")
        status = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["parent", "company"],
                    condition=models.Q(status="live"),
                    name="one_null_grouped",
                    **_NULLS_TOGETHER,
                ),
            ]

    class TwoRules(models.Model):
        """Two constraints, and the NULLs are counted together under only one of them."""

        parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
        name = models.CharField(max_length=50)
        code = models.CharField(max_length=50, null=True)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["parent", "name"], name="one_name_apart"),
                models.UniqueConstraint(fields=["code"], name="one_code", **_NULLS_TOGETHER),
            ]

    class NullsApart(models.Model):
        """The default NULL handling, spelled out."""

        parent = models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")
        name = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["parent", "name"],
                    name="one_apart_folder",
                    **({"nulls_distinct": True} if django.VERSION >= (5, 0) else {}),
                ),
            ]


def _tree() -> Tree:
    return Tree(roots=0.05, sizes=Zipf())


def _companies(rows: int = 50) -> Table:
    return Table(Company, rows=rows, name=Constant("c"))


def _people() -> Table:
    return Table(Person, rows=50, name=Constant("p"))


def _refusal(*tables: Table) -> str:
    with pytest.raises(InvalidShape) as raised:
        Shape(*tables)
    return str(raised.value)


def _assert_names_only_what_a_tree_accepts(message: str) -> None:
    # Table refuses all three over a tree, so a refusal offering one of them
    # would send the reader from one refusal straight to another.
    assert "scope='group'" not in message
    assert "Paired(" not in message
    assert "PerParent(" not in message
    assert "Distinct" in message
    assert "Projection" in message


# A tree that provably gives no row two children.


def _flat() -> Tree:
    return Tree(roots=0.05, sizes=Constant(1))


def _folders(parent: Tree, rows: int) -> Table:
    return Table(Folder, rows=rows, parent=parent, name=Skew({"a": 1, "b": 1}), code=Constant("c"))


@pytest.mark.parametrize("rows", [1, 2, 3, 20, 21, 199, 200, 1000])
@pytest.mark.parametrize("roots", [0.01, 0.05, 0.5, 0.95])
@pytest.mark.parametrize("seed", [0, 7])
@pytest.mark.parametrize(
    ("table", "field"),
    [("testapp_branch", "parent"), ("testapp_edge", "left"), ("testapp_folder", "parent")],
)
def test_a_flat_tree_gives_no_row_two_children(
    rows: int, roots: float, seed: int, table: str, field: str
) -> None:
    # What the exemptions below stand on, read from what the build uses. The
    # table and field are varied because every other tree figure moves with them.
    plan = resolve_tree(Tree(roots=roots, sizes=Constant(1)), rows, seed, table, field)
    parents = [plan.parent_for(row) for row in range(rows)]
    children = [parent for parent in parents if parent is not None]

    assert len(children) == len(set(children))


def test_a_flat_tree_of_no_weight_is_refused_when_it_is_resolved() -> None:
    # Reads as one value, so no constraint refuses it, and it has no children to
    # share out: the refusal is the build's, before any row reaches COPY.
    Shape(_folders(Tree(roots=0.05, sizes=Constant(0)), rows=20))

    with pytest.raises(InvalidShape, match="sum to 0.0"):
        resolve_tree(Tree(roots=0.05, sizes=Constant(0)), 20, 0, "testapp_folder", "parent")


def test_a_flat_tree_beside_a_fan_out_is_accepted() -> None:
    # Five parents cannot make the fan-out's own proof at 500 rows, so the tree
    # is the only thing keeping this constraint.
    Shape(_companies(5), Table(Branch, rows=500, parent=_flat(), company=FanOut(Zipf())))


def test_a_flat_tree_beside_a_second_tree_is_accepted_in_either_position() -> None:
    Shape(Table(Edge, rows=500, left=_flat(), right=_tree()))
    Shape(Table(Edge, rows=500, left=_tree(), right=_flat()))


def test_a_flat_tree_beside_a_fan_out_and_a_draw_is_accepted() -> None:
    Shape(
        _companies(5),
        Table(
            Unit,
            rows=500,
            parent=_flat(),
            company=FanOut(Zipf()),
            label=Skew({"a": 1, "b": 1}),
        ),
    )


def test_a_flat_tree_beside_a_draw_is_accepted() -> None:
    Shape(_folders(_flat(), rows=500))


def test_a_skew_of_one_value_is_as_flat_as_a_constant() -> None:
    Shape(_folders(Tree(roots=0.05, sizes=Skew({3: 1})), rows=500))


def test_a_flat_tree_in_a_conditional_uniqueness_is_accepted() -> None:
    Shape(Table(Reply, rows=500, parent=_flat(), status=Constant("accepted")))
    Shape(Table(Reply, rows=500, parent=_flat(), status=Skew({"accepted": 1, "open": 1})))


def test_a_flat_tree_beside_a_fan_out_in_a_conditional_uniqueness_is_accepted() -> None:
    # This constraint has a fan-out in it, so it would go to the fan-out's
    # conditional check, and the tree answers it before that.
    Shape(
        _companies(),
        Table(
            Grouped,
            rows=200,
            parent=_flat(),
            company=FanOut(Zipf()),
            status=Skew({"live": 1, "dead": 1}),
        ),
    )


def _refusal_alone(model: type[models.Model], rows: int, tree: Tree) -> str:
    # Table itself refuses unique=True, so its construction is inside the block.
    with pytest.raises(InvalidShape) as raised:
        Shape(Table(model, rows=rows, previous=tree))
    return str(raised.value)


def test_a_flat_tree_alone_under_a_uniqueness_is_accepted() -> None:
    # Every spelling of a unique self-key is one fact, so the proof that holds
    # beside a draw holds with nothing beside it, as FanOut(max_children=1) does
    # on a unique column. Through Shape, since Table cannot see a constraint.
    with isolate_apps("tests.testapp"):

        class Alone(models.Model):
            previous = models.ForeignKey(
                "self", null=True, on_delete=models.CASCADE, related_name="+"
            )

            class Meta:
                app_label = "testapp"
                constraints = [
                    models.UniqueConstraint(fields=["previous"], name="one_link_alone"),
                ]

        Shape(Table(Alone, rows=50, previous=_flat()))
    for model in (Chain, Relay, Successor):
        Shape(Table(model, rows=50, previous=_flat()))
        Shape(Table(model, rows=50, previous=Tree(roots=0.05, sizes=Skew({3: 1}))))


@pytest.mark.parametrize(
    ("model", "names"),
    [
        (Chain, "one_link_after_each"),
        (Relay, "unique_together ('previous',)"),
        (Successor, "Successor.previous is unique"),
    ],
)
def test_a_tree_of_many_sizes_alone_stays_refused_and_names_the_flat_tree(
    model: type[models.Model], names: str
) -> None:
    message = _refusal_alone(model, 50, Tree(roots=0.2, sizes=Zipf()))

    assert names in message
    assert "previous=Tree(roots=0.2, sizes=Constant(1))" in message
    assert "fill it after the build" in message


@pytest.mark.parametrize("model", [Chain, Relay, Successor])
def test_a_tree_of_two_sizes_alone_stays_refused(model: type[models.Model]) -> None:
    message = _refusal_alone(model, 50, Tree(roots=0.2, sizes=Skew({1: 1, 2: 1})))

    assert "sizes=Constant(1))" in message


@pytest.mark.parametrize("model", [Chain, Relay, Successor])
def test_a_table_too_small_for_siblings_is_accepted_alone(model: type[models.Model]) -> None:
    # The arithmetic half of the proof, on each spelling: one child has no sibling.
    Shape(Table(model, rows=2, previous=Tree(roots=0.5, sizes=Zipf())))


@pytest.mark.parametrize("model", [Chain, Relay, Successor])
def test_a_table_with_two_children_is_refused_alone(model: type[models.Model]) -> None:
    message = _refusal_alone(model, 3, Tree(roots=0.1, sizes=Zipf()))

    assert "sizes=Constant(1))" in message


def test_a_unique_self_key_that_refuses_null_stays_refused_however_flat() -> None:
    # A root is NULL and the first row is always one, whichever spelling makes
    # the key unique, so no proof about children reaches this.
    with isolate_apps("tests.testapp"):

        class Required(models.Model):
            previous = models.ForeignKey("self", on_delete=models.CASCADE, related_name="+")

            class Meta:
                app_label = "testapp"
                constraints = [
                    models.UniqueConstraint(fields=["previous"], name="one_required_link"),
                ]

        with pytest.raises(InvalidShape, match="Required.previous cannot be null"):
            Table(Required, rows=50, previous=_flat())
    with pytest.raises(InvalidShape, match="RootlessLink.previous cannot be null"):
        Table(RootlessLink, rows=50, previous=_flat())


_NEEDS_NULLS_DISTINCT = pytest.mark.skipif(
    django.VERSION < (5, 0), reason="UniqueConstraint(nulls_distinct=) arrived in Django 5.0"
)


@_NEEDS_NULLS_DISTINCT
@pytest.mark.parametrize(
    ("tree", "rows"),
    [
        (Tree(roots=0.05, sizes=Constant(1)), 300),
        (Tree(roots=0.05, sizes=Zipf()), 300),
        (Tree(roots=0.95, sizes=Zipf()), 20),
    ],
)
def test_roots_counted_as_one_group_collide_alone(tree: Tree, rows: int) -> None:
    # The roots hold one NULL between them, so the proof that a flat tree keeps a
    # uniqueness is false here, and leaving the column out loads that NULL in
    # every row.
    message = _refusal(Table(TiedLink, rows=rows, previous=tree))

    assert "one_tied_link_after_each" in message
    assert "nulls_distinct=False" in message
    assert "Projection" in message
    # The refusal must not offer the shape it has just refused, or the other one.
    assert "Constant(1))," not in message
    assert "previous=Tree(" not in message
    assert "fill it after the build" not in message


@_NEEDS_NULLS_DISTINCT
@pytest.mark.parametrize(
    ("tree", "rows"),
    [(Tree(roots=0.05, sizes=Constant(1)), 300), (Tree(roots=0.95, sizes=Zipf()), 20)],
)
def test_roots_counted_as_one_group_collide_beside_a_draw(tree: Tree, rows: int) -> None:
    # Every root holds the same NULL, so the roots collide among themselves
    # however few children the tree gives anyone.
    message = _refusal(Table(NullFolder, rows=rows, parent=tree, name=Skew({"a": 1, "b": 1})))

    assert "one_null_folder" in message
    assert "is a Tree beside" in message
    # The refusal must not offer the shape it has just refused.
    assert "Constant(1)," not in message


@_NEEDS_NULLS_DISTINCT
def test_roots_counted_as_one_group_collide_beside_a_fan_out() -> None:
    message = _refusal(
        _companies(5), Table(NullBranch, rows=500, parent=_flat(), company=FanOut(Zipf()))
    )

    assert "one_null_branch" in message
    assert "Constant(1)," not in message
    # Left out of the shape, the tree's NULLs are one group here as well.
    assert "its own group" not in message


@_NEEDS_NULLS_DISTINCT
def test_roots_counted_as_one_group_collide_in_a_conditional_uniqueness() -> None:
    message = _refusal(Table(NullReply, rows=500, parent=_flat(), status=Constant("accepted")))

    assert "one_null_reply" in message
    assert "Constant(1)," not in message


@_NEEDS_NULLS_DISTINCT
def test_roots_counted_as_one_group_collide_beside_a_fan_out_in_a_conditional() -> None:
    message = _refusal(
        _companies(),
        Table(
            NullGrouped,
            rows=200,
            parent=_flat(),
            company=FanOut(Zipf()),
            status=Skew({"live": 1, "dead": 1}),
        ),
    )

    assert "one_null_grouped" in message


@_NEEDS_NULLS_DISTINCT
def test_nulls_counted_together_under_another_constraint_change_nothing_here() -> None:
    # The reading is per constraint, because the NULLs of one index say nothing
    # about another's.
    # The second is declared because a column left out of it holds one NULL in every
    # row, which no load survives.
    Shape(
        Table(
            TwoRules, rows=300, parent=_flat(), name=Skew({"a": 1, "b": 1}), code=Sequential(1, 1)
        )
    )


@_NEEDS_NULLS_DISTINCT
def test_nulls_distinct_spelled_out_is_the_default() -> None:
    Shape(Table(NullsApart, rows=20, parent=_flat(), name=Constant("same")))


def test_a_tree_of_two_sizes_is_not_flat() -> None:
    # Enumerable is not enough: the proof needs every row to weigh the same.
    tree = Tree(roots=0.05, sizes=Skew({1: 1, 50: 1}))

    for message in (
        _refusal(_companies(), Table(Branch, rows=500, parent=tree, company=FanOut(Zipf()))),
        _refusal(_folders(tree, rows=500)),
        _refusal(Table(Reply, rows=500, parent=tree, status=Constant("accepted"))),
    ):
        assert "Constant(1)" in message


def test_a_tree_with_one_child_has_no_siblings_to_collide() -> None:
    # 19 roots of 20 leave one child, whatever the sizes weigh.
    tree = Tree(roots=0.95, sizes=Zipf())

    Shape(_companies(), Table(Branch, rows=20, parent=tree, company=FanOut(Zipf())))
    Shape(_folders(tree, rows=20))
    Shape(Table(Reply, rows=20, parent=tree, status=Constant("accepted")))


@pytest.mark.parametrize("rows", [1, 2])
def test_a_table_too_small_for_siblings_is_accepted(rows: int) -> None:
    Shape(
        _companies(),
        Table(Branch, rows=rows, parent=_tree(), company=FanOut(Zipf())),
    )


def test_a_tree_with_two_children_can_give_them_one_parent() -> None:
    # 18 roots of 20 leave two, and that is where the arithmetic stops proving it.
    message = _refusal(
        _companies(),
        Table(Branch, rows=20, parent=Tree(roots=0.9, sizes=Zipf()), company=FanOut(Zipf())),
    )

    assert "one_branch_per_pair" in message


# A tree beside a fan-out.


def test_a_tree_beside_a_fan_out_is_refused() -> None:
    message = _refusal(
        _companies(), Table(Branch, rows=200, parent=_tree(), company=FanOut(Zipf()))
    )

    assert "one_branch_per_pair" in message
    assert "Branch.parent is a Tree" in message
    assert "Branch.company is a fan-out" in message
    assert "max_children=1" in message
    assert "Constant(1)" in message
    _assert_names_only_what_a_tree_accepts(message)


def test_a_tree_beside_a_fan_out_with_a_cap_of_one_is_accepted() -> None:
    # A pair is distinct as soon as either half is, and a cap of one never
    # repeats a company. This is the exemption the fan-out side can prove.
    Shape(
        _companies(300),
        Table(Branch, rows=200, parent=_tree(), company=FanOut(Zipf(), max_children=1)),
    )


def test_a_tree_beside_a_flat_fan_out_over_enough_parents_is_accepted() -> None:
    Shape(
        _companies(200),
        Table(Branch, rows=200, parent=_tree(), company=FanOut(Constant(1))),
    )


def test_a_tree_beside_a_flat_fan_out_one_row_over_is_refused() -> None:
    # One row more than parents gives some parent two rows, and the exemption
    # stops there. It reads the fan-out and never the tree.
    message = _refusal(
        _companies(199),
        Table(Branch, rows=200, parent=_tree(), company=FanOut(Constant(1))),
    )

    assert "one_branch_per_pair" in message


def test_a_tree_beside_a_fan_out_and_a_draw_is_refused() -> None:
    message = _refusal(
        _companies(),
        Table(
            Unit,
            rows=200,
            parent=_tree(),
            company=FanOut(Zipf()),
            label=Skew({"a": 1, "b": 1}),
        ),
    )

    assert "one_unit_per_triple" in message
    assert "Unit.label=" in message


def test_a_tree_beside_a_fan_out_and_a_distinct_column_is_accepted() -> None:
    Shape(
        _companies(),
        Table(Unit, rows=200, parent=_tree(), company=FanOut(Zipf()), label=Sequential(1, 1)),
    )


def test_a_tree_beside_a_fan_out_and_a_column_that_does_not_move_is_refused() -> None:
    # The protocol answers for the parameters and not the class. Without the
    # is_distinct_per_row() conjunct, a zero step is accepted here.
    message = _refusal(
        _companies(),
        Table(Unit, rows=200, parent=_tree(), company=FanOut(Zipf()), label=Sequential(0, 0)),
    )

    assert "one_unit_per_triple" in message


def test_a_tree_beside_a_fan_out_and_a_derivation_is_left_to_the_other_two_nets() -> None:
    # A group-scoped derivation over the fan-out can number a company's rows,
    # which keeps the triple apart whatever the tree does. Its compute= is not
    # readable, so a refusal here could be the wrong one.
    Shape(
        _companies(),
        Table(
            Unit,
            rows=200,
            parent=_tree(),
            company=FanOut(Zipf()),
            label=Derived("company", compute=lambda position, size: position, scope="group"),
        ),
    )


def test_a_tree_beside_a_fan_out_and_an_undeclared_column_is_left_alone() -> None:
    # The column loads NULL, and each NULL in a unique index is its own group.
    Shape(_companies(), Table(Unit, rows=200, parent=_tree(), company=FanOut(Zipf())))


def test_a_tree_and_two_fan_outs_keep_the_refusal_that_can_offer_a_pairing() -> None:
    with isolate_apps("tests.testapp"):

        class Triple(models.Model):
            parent = models.ForeignKey(
                "self", null=True, on_delete=models.CASCADE, related_name="+"
            )
            company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="+")
            person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="+")

            class Meta:
                app_label = "testapp"
                constraints = [
                    models.UniqueConstraint(
                        fields=["parent", "company", "person"], name="one_triple"
                    ),
                ]

        message = _refusal(
            _companies(),
            _people(),
            Table(
                Triple,
                rows=200,
                parent=_tree(),
                company=FanOut(Zipf()),
                person=FanOut(Zipf()),
            ),
        )
        Shape(
            _companies(),
            _people(),
            Table(
                Triple,
                rows=200,
                parent=_tree(),
                company=FanOut(Zipf()),
                person=Paired("company", Zipf()),
            ),
        )

    assert "are fan-outs" in message
    assert "person=Paired('company', Zipf())" in message


# Two trees.


def test_two_trees_under_one_uniqueness_are_refused_as_two_trees() -> None:
    # This is what keeps `len(trees) != 1` in the drawn-column check. Loosened
    # to `not trees`, two trees alone reach that check's "tree alone" branch
    # and come back as a statement about one column.
    message = _refusal(Table(Edge, rows=200, left=_tree(), right=_tree()))

    assert "one_edge_per_pair" in message
    assert "Edge.left is a Tree" in message
    assert "Edge.right is a Tree" in message
    assert "permits one row per" not in message
    assert "max_children" not in message
    _assert_names_only_what_a_tree_accepts(message)


def test_two_trees_beside_a_draw_are_refused_as_two_trees() -> None:
    # The same guard from its other side: loosened, this one comes back as one
    # tree beside a draw, naming the second tree as though it were drawn.
    message = _refusal(
        Table(LabelledEdge, rows=200, left=_tree(), right=_tree(), label=Skew({"a": 1, "b": 1}))
    )

    assert "one_edge_per_triple" in message
    assert "LabelledEdge.left is a Tree" in message
    assert "LabelledEdge.right is a Tree" in message
    assert "LabelledEdge.label=" in message


def test_two_trees_beside_a_distinct_column_are_accepted() -> None:
    Shape(Table(LabelledEdge, rows=200, left=_tree(), right=_tree(), label=Sequential(1, 1)))


def test_two_trees_beside_a_derivation_are_left_to_the_other_two_nets() -> None:
    Shape(
        Table(
            LabelledEdge,
            rows=200,
            left=_tree(),
            right=_tree(),
            label=Derived("left", compute=lambda left: f"{left}-x"),
        )
    )


def test_two_trees_beside_an_undeclared_column_are_left_alone() -> None:
    Shape(Table(LabelledEdge, rows=200, left=_tree(), right=_tree()))


def test_one_tree_with_the_second_key_left_out_is_accepted() -> None:
    # The remedy the refusal offers: a key left out loads NULL.
    Shape(Table(Edge, rows=200, left=_tree()))


def test_one_fan_out_with_the_tree_left_out_is_not_this_refusal() -> None:
    # No tree in the constraint, so nothing here can name one.
    Shape(_companies(), Table(Branch, rows=200, company=FanOut(Zipf())))


# A tree in a conditional uniqueness.


def test_a_tree_in_a_conditional_uniqueness_is_refused() -> None:
    message = _refusal(
        Table(Reply, rows=200, parent=_tree(), status=Skew({"accepted": 1, "open": 9}))
    )

    assert "one_accepted_reply" in message
    assert "Reply.parent is a Tree" in message
    assert "Reply.status=" in message
    assert "asks for 20 of them" in message
    assert "Constant(1)" in message
    _assert_names_only_what_a_tree_accepts(message)


def test_the_set_spelling_of_a_condition_is_read_the_same_way() -> None:
    with isolate_apps("tests.testapp"):

        class Closed(models.Model):
            parent = models.ForeignKey(
                "self", null=True, on_delete=models.CASCADE, related_name="+"
            )
            status = models.CharField(max_length=50)

            class Meta:
                app_label = "testapp"
                constraints = [
                    models.UniqueConstraint(
                        fields=["parent"],
                        condition=models.Q(status__in=["won", "lost"]),
                        name="one_closed",
                    ),
                ]

        message = _refusal(
            Table(Closed, rows=200, parent=_tree(), status=Skew({"won": 1, "open": 9}))
        )
        Shape(Table(Closed, rows=200, parent=_tree(), status=Constant("open")))

    assert "one_closed" in message


def test_a_condition_no_row_can_land_inside_is_accepted() -> None:
    Shape(Table(Reply, rows=200, parent=_tree(), status=Skew({"open": 1, "closed": 1})))


def test_an_undeclared_condition_column_is_accepted() -> None:
    # It loads NULL, and NULL satisfies no equality.
    Shape(Table(Reply, rows=200, parent=_tree()))


def test_a_condition_this_module_cannot_read_is_left_alone() -> None:
    with isolate_apps("tests.testapp"):

        class Negated(models.Model):
            parent = models.ForeignKey(
                "self", null=True, on_delete=models.CASCADE, related_name="+"
            )
            status = models.CharField(max_length=50)

            class Meta:
                app_label = "testapp"
                constraints = [
                    models.UniqueConstraint(
                        fields=["parent"],
                        condition=~models.Q(status="open"),
                        name="one_not_open",
                    ),
                ]

        Shape(Table(Negated, rows=200, parent=_tree(), status=Skew({"open": 1, "closed": 1})))


def test_a_distribution_that_cannot_enumerate_itself_is_read_as_able_to() -> None:
    # Unknown is read as a yes, as it is on the fan-out side, and the message
    # has no share to quote.
    message = _refusal(Table(Reply, rows=200, parent=_tree(), status=Sequential(1, 1)))

    assert "one_accepted_reply" in message
    assert "draws 'accepted' independently per row" in message


def test_a_rule_kept_per_a_fan_out_outside_the_constraint_is_refused() -> None:
    # One accepted reply per company says nothing about how many one parent
    # gets, and pointing at the constraint's own fields would be pointing at
    # the tree, which Table refuses to decide anything by.
    message = _refusal(
        _companies(),
        Table(
            Reply,
            rows=200,
            parent=_tree(),
            company=FanOut(Zipf()),
            status=PerParent("company", last="accepted", rest="open"),
        ),
    )

    # The declaration is quoted back, so its own PerParent is in the message.
    # What must not be there is one offered over the tree.
    assert "one_accepted_reply" in message
    assert "decided per company" in message
    assert "PerParent('parent'" not in message
    assert "scope='group'" not in message
    assert "Distinct" in message
    assert "Projection" in message


def test_a_rule_kept_per_a_fan_out_with_nothing_inside_the_condition_is_accepted() -> None:
    Shape(
        _companies(),
        Table(
            Reply,
            rows=200,
            parent=_tree(),
            company=FanOut(Zipf()),
            status=PerParent("company", last="closed", rest="open"),
        ),
    )


def test_a_rule_kept_per_a_fan_out_whose_rest_lands_inside_the_condition_is_refused() -> None:
    # The special row is outside and the rest is inside, which is every other
    # row. Reading only the special row accepts this.
    message = _refusal(
        _companies(),
        Table(
            Reply,
            rows=200,
            parent=_tree(),
            company=FanOut(Zipf()),
            status=PerParent("company", last="closed", rest="accepted"),
        ),
    )

    assert "one_accepted_reply" in message


def test_a_derivation_filling_the_condition_column_is_left_to_the_other_two_nets() -> None:
    # Its compute= may never land inside the condition, and that is not
    # readable from here. The one derivation that can be read, PerParent, is
    # read above.
    Shape(
        Table(
            Reply,
            rows=200,
            parent=_tree(),
            note=Skew({"a": 1, "b": 1}),
            status=Derived("note", compute=lambda note: "open"),
        )
    )


def test_a_tree_beside_a_draw_in_a_conditional_uniqueness_is_refused() -> None:
    message = _refusal(
        Table(
            Tagged,
            rows=200,
            parent=_tree(),
            tag=Skew({"a": 1, "b": 1}),
            status=Skew({"live": 1, "dead": 1}),
        )
    )

    assert "one_live_tag" in message
    assert "Tagged.parent is a Tree" in message


def test_a_tree_beside_a_distinct_column_in_a_conditional_uniqueness_is_accepted() -> None:
    # The remedy the refusal names, and why it has to be honoured: every group
    # holds one row, so the condition has nothing to count.
    Shape(
        Table(
            Tagged,
            rows=200,
            parent=_tree(),
            tag=Sequential(1, 1),
            status=Skew({"live": 1, "dead": 1}),
        )
    )


def test_a_tree_beside_a_column_that_does_not_move_in_a_conditional_is_refused() -> None:
    message = _refusal(
        Table(
            Tagged,
            rows=200,
            parent=_tree(),
            tag=Sequential(0, 0),
            status=Skew({"live": 1, "dead": 1}),
        )
    )

    assert "one_live_tag" in message


def test_a_tree_beside_an_undeclared_column_in_a_conditional_is_left_alone() -> None:
    # Each NULL in the second column is its own group.
    Shape(Table(Tagged, rows=200, parent=_tree(), status=Skew({"live": 1, "dead": 1})))


def test_a_tree_beside_a_derivation_in_a_conditional_is_left_to_the_other_two_nets() -> None:
    Shape(
        Table(
            Tagged,
            rows=200,
            parent=_tree(),
            tag=Derived("status", compute=lambda status: f"{status}-x"),
            status=Skew({"live": 1, "dead": 1}),
        )
    )


def test_two_trees_in_a_conditional_uniqueness_are_refused() -> None:
    message = _refusal(
        Table(Crossed, rows=200, left=_tree(), right=_tree(), status=Skew({"live": 1, "dead": 1}))
    )

    assert "one_live_edge" in message
    assert "Crossed.left is a Tree" in message
    assert "Crossed.right is a Tree" in message


def test_a_conditional_uniqueness_over_no_tree_and_no_fan_out_stays_undecided() -> None:
    with isolate_apps("tests.testapp"):

        class Plain(models.Model):
            tag = models.CharField(max_length=50)
            status = models.CharField(max_length=50)

            class Meta:
                app_label = "testapp"
                constraints = [
                    models.UniqueConstraint(
                        fields=["tag"],
                        condition=models.Q(status="live"),
                        name="one_live",
                    ),
                ]

        Shape(
            Table(Plain, rows=200, tag=Skew({"a": 1, "b": 1}), status=Skew({"live": 1, "dead": 1}))
        )


def test_a_conditional_uniqueness_over_a_tree_and_a_fan_out_names_the_fan_out() -> None:
    # The fan-out's rule still applies, since one row per company is enough.
    # The remedy has to name the fan-out wherever it sits in the constraint:
    # the tree comes first here, and a PerParent over it is refused by Table.
    message = _refusal(
        _companies(),
        Table(
            Grouped,
            rows=200,
            parent=_tree(),
            company=FanOut(Zipf()),
            status=Skew({"live": 1, "dead": 1}),
        ),
    )

    assert "one_live_per_pair" in message
    assert "PerParent('company', last='live'" in message
    assert "PerParent('parent'" not in message


def test_a_conditional_uniqueness_over_a_tree_kept_per_its_fan_out_is_accepted() -> None:
    Shape(
        _companies(),
        Table(
            Grouped,
            rows=200,
            parent=_tree(),
            company=FanOut(Zipf()),
            status=PerParent("company", last="live", rest="dead"),
        ),
    )
