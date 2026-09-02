"""One row of every group is different, and the rest are not."""

from __future__ import annotations

import enum

import pytest

from django_data_shape import Constant, InvalidShape, PerParent, Scope, Skew, Uniform


def _positions(per_parent: PerParent, size: int) -> list[object]:
    """What every row of one group of ``size`` children gets, in group order."""
    return [
        per_parent.value(row=position, draw=0.5, sources=((position, size),))
        for position in range(size)
    ]


def test_the_last_row_of_a_group_is_the_special_one() -> None:
    per_parent = PerParent("company", last="ACTIVE", rest="COMPLETE")

    assert _positions(per_parent, 4) == ["COMPLETE", "COMPLETE", "COMPLETE", "ACTIVE"]


def test_the_first_row_of_a_group_is_the_special_one_when_asked() -> None:
    per_parent = PerParent("company", first="PRIMARY", rest="OTHER")

    assert _positions(per_parent, 3) == ["PRIMARY", "OTHER", "OTHER"]


def test_a_group_of_one_gets_exactly_one_special_row() -> None:
    # The arithmetic that makes "one active project per company" hold rather
    # than a case anybody had to write: a company with one project has that one
    # project active, which is what the rule says.
    per_parent = PerParent("company", last="ACTIVE", rest="COMPLETE")

    assert _positions(per_parent, 1) == ["ACTIVE"]


def test_a_group_smaller_than_the_count_is_all_special() -> None:
    # Not a clamp anybody wrote either. Three winners out of two entries is two
    # winners, and the alternative -- refusing, or leaving a placing empty --
    # would be inventing a rule the declaration did not state.
    per_parent = PerParent("contest", last="WON", rest="LOST", count=3)

    assert _positions(per_parent, 2) == ["WON", "WON"]


def test_several_winners_per_group() -> None:
    per_parent = PerParent("contest", last="WON", rest="LOST", count=2)

    assert _positions(per_parent, 5) == ["LOST", "LOST", "LOST", "WON", "WON"]


def test_none_is_a_value_the_special_row_can_take() -> None:
    # The whole reason last= and first= need a sentinel rather than defaulting
    # to None: an SCD-2 chain says the current period is the one with no end,
    # and "the last row holds nothing" has to be distinguishable from "no last
    # row was declared".
    per_parent = PerParent("company", last=None, rest="2026-01-01")

    assert _positions(per_parent, 2) == ["2026-01-01", None]


def test_the_rest_may_be_drawn_from_a_distribution_that_enumerates_itself() -> None:
    per_parent = PerParent("company", last="ACTIVE", rest=Skew({"COMPLETE": 1, "CANCELLED": 1}))

    assert per_parent.value(row=0, draw=0.1, sources=((0, 3),)) == "COMPLETE"
    assert per_parent.value(row=1, draw=0.9, sources=((1, 3),)) == "CANCELLED"
    assert per_parent.value(row=2, draw=0.9, sources=((2, 3),)) == "ACTIVE"


def test_a_rest_that_cannot_say_what_it_emits_is_refused() -> None:
    # Uniform is a perfectly good distribution and the wrong one here: nothing
    # can ask it whether it might also produce the special value, so the count
    # of special rows would stop being one per group and start being a hope.
    with pytest.raises(InvalidShape, match="cannot say which values it produces"):
        PerParent("company", last="ACTIVE", rest=Uniform(0, 1))


def test_a_rest_that_lists_the_special_value_is_refused() -> None:
    with pytest.raises(InvalidShape, match="lists 'ACTIVE' as well"):
        PerParent("company", last="ACTIVE", rest=Skew({"ACTIVE": 0.1, "COMPLETE": 0.9}))


def test_a_constant_rest_repeating_the_special_value_is_refused() -> None:
    with pytest.raises(InvalidShape, match="lists 'ACTIVE' as well"):
        PerParent("company", last="ACTIVE", rest=Constant("ACTIVE"))


def test_an_enum_member_is_a_value_rather_than_a_distribution() -> None:
    # The trap the structural test is written around. An Enum member carries a
    # ``value``, so a check made on the instance would refuse every enum-valued
    # status column -- and enums are the ordinary way to write one.
    class Status(enum.Enum):
        ACTIVE = "ACTIVE"
        COMPLETE = "COMPLETE"

    per_parent = PerParent("company", last=Status.ACTIVE, rest=Status.COMPLETE)

    assert _positions(per_parent, 2) == [Status.COMPLETE, Status.ACTIVE]


def test_neither_end_declared_is_refused() -> None:
    with pytest.raises(InvalidShape, match="was given neither"):
        PerParent("company", rest="COMPLETE")


def test_both_ends_declared_is_refused() -> None:
    with pytest.raises(InvalidShape, match="was given both"):
        PerParent("company", last="ACTIVE", first="PRIMARY", rest="COMPLETE")


@pytest.mark.parametrize("count", [0, -1, 1.5, True])
def test_a_count_below_one_or_not_a_whole_number_is_refused(count: object) -> None:
    with pytest.raises(InvalidShape, match="count of at least one"):
        PerParent("company", last="ACTIVE", rest="COMPLETE", count=count)


def test_it_reads_its_group_from_the_fan_out_it_names() -> None:
    per_parent = PerParent("company", last="ACTIVE", rest="COMPLETE")

    assert per_parent.scope is Scope.GROUP
    assert per_parent.sources == ("company",)
    assert per_parent.relation == "company"


def test_it_exposes_what_the_constraint_pre_check_has_to_read() -> None:
    per_parent = PerParent("company", last="ACTIVE", rest="COMPLETE", count=1, order_by="at")

    assert per_parent.special == "ACTIVE"
    assert per_parent.rest == "COMPLETE"
    assert per_parent.count == 1
    assert per_parent.order_by == "at"


def test_the_two_ends_are_different_declarations_to_the_digest() -> None:
    last = PerParent("company", last="ACTIVE", rest="COMPLETE")
    first = PerParent("company", first="ACTIVE", rest="COMPLETE")

    assert last.canonical() != first.canonical()


def test_order_by_is_part_of_the_declaration_even_though_it_moves_no_value() -> None:
    # It changes which declarations are accepted, and a shape that would be
    # refused is not the same shape as one that would not.
    plain = PerParent("company", last="ACTIVE", rest="COMPLETE")
    ordered = PerParent("company", last="ACTIVE", rest="COMPLETE", order_by="created_at")

    assert plain.canonical() != ordered.canonical()


def test_it_reads_back_as_what_was_declared() -> None:
    assert repr(PerParent("company", last="ACTIVE", rest="COMPLETE")) == (
        "PerParent('company', last='ACTIVE', rest='COMPLETE', count=1, order_by=None)"
    )
    assert repr(PerParent("company", first="PRIMARY", rest="OTHER", count=2)) == (
        "PerParent('company', first='PRIMARY', rest='OTHER', count=2, order_by=None)"
    )
