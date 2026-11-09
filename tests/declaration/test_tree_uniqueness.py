"""A tree under a uniqueness that nothing arranges, refused when the shape is declared.

The models are declared here under ``isolate_apps`` rather than in the test app,
because every test below stops at ``Shape()`` and none needs a table. It also
keeps this module importable against a tree without the refusals, where each
refusal test fails on its assertion rather than dying at import.
"""

from __future__ import annotations

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
from tests.testapp.models import Company, Person

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


# A tree beside a fan-out.


def test_a_tree_beside_a_fan_out_is_refused() -> None:
    message = _refusal(
        _companies(), Table(Branch, rows=200, parent=_tree(), company=FanOut(Zipf()))
    )

    assert "one_branch_per_pair" in message
    assert "Branch.parent is a Tree" in message
    assert "Branch.company is a fan-out" in message
    assert "max_children=1" in message
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
