"""The fan-out declaration and its refusals."""

from __future__ import annotations

from typing import Any, cast

import pytest

from django_data_shape import Constant, FanOut, InvalidShape, Uniform, Zipf


def test_it_reads_back_as_what_was_declared() -> None:
    assert repr(FanOut(Zipf(1.2))) == (
        "FanOut(Zipf(1.2), childless=0.0, null=0.0, placement='arrival')"
    )


def test_arrival_is_the_default_placement() -> None:
    # The default has to be the honest one. Emitting children parent by parent
    # gives a perfectly clustered table no production system has, and it
    # flatters every index scan over the foreign key.
    assert FanOut(Zipf()).placement == "arrival"


@pytest.mark.parametrize("share", [-0.1, 1.0, 1.5])
def test_a_share_outside_the_unit_interval_is_refused(share: float) -> None:
    with pytest.raises(InvalidShape, match="share of the whole"):
        FanOut(Zipf(), childless=share)
    with pytest.raises(InvalidShape, match="share of the whole"):
        FanOut(Zipf(), null=share)


def test_an_unknown_placement_is_refused_and_the_options_listed() -> None:
    with pytest.raises(InvalidShape, match="arrival, grouped") as raised:
        FanOut(Zipf(), placement="clustered")

    assert "'clustered'" in str(raised.value)


def test_any_positive_distribution_can_size_the_partition() -> None:
    # Zipf is the realistic one, not the only one: a flatter spread is a
    # legitimate declaration for a table that genuinely has one.
    assert FanOut(Uniform(1, 10)).sizes is not None
    assert FanOut(Constant(1)).childless == 0.0


def test_parents_narrows_the_partition_to_the_keys_it_names() -> None:
    fan_out = FanOut(Zipf(), parents=[7, 9])

    assert fan_out.parents == (7, 9)


def test_no_parents_declared_is_every_parent_there_is() -> None:
    assert FanOut(Zipf()).parents is None


def test_an_empty_parent_list_is_refused_rather_than_read_as_all_of_them() -> None:
    # The dangerous reading. An empty sequence is falsy, so treating it as "not
    # declared" would spread the table over every parent -- the opposite of what
    # was asked, silently, in the one declaration whose whole point is narrowing.
    with pytest.raises(InvalidShape, match="parents= names no parent"):
        FanOut(Zipf(), parents=[])


def test_a_parent_named_twice_is_refused() -> None:
    # Not deduplicated. A key named twice would weigh that parent twice, so the
    # partition would not be the one the caller wrote -- and the likeliest cause
    # is a list built by a loop that ran once too often.
    with pytest.raises(InvalidShape, match="names 7 more than once"):
        FanOut(Zipf(), parents=[7, 9, 7])


def test_parents_are_part_of_what_the_declaration_says() -> None:
    # The template-database cache keys on canonical(), and two shapes differing
    # only in which parents they spread over are two different worlds.
    # One distribution shared, or two Zipf() instances would differ on their own.
    sizes = Zipf()
    assert FanOut(sizes, parents=[1]).canonical() == FanOut(sizes, parents=[1]).canonical()
    assert FanOut(sizes, parents=[1]).canonical() != FanOut(sizes, parents=[2]).canonical()
    assert FanOut(sizes, parents=[1]).canonical() != FanOut(sizes).canonical()


def test_parents_reads_back_in_the_repr() -> None:
    assert "parents=(7, 9)" in repr(FanOut(Zipf(), parents=[7, 9]))


def test_a_fan_out_is_uncapped_unless_a_cap_is_declared() -> None:
    assert FanOut(Zipf()).max_children is None
    assert FanOut(Zipf(), max_children=5).max_children == 5


@pytest.mark.parametrize("cap", [0, -1, 2.5, "5", True])
def test_a_cap_that_is_not_a_positive_count_is_refused(cap: object) -> None:
    # True is an int equal to 1, and a cap of one written as True has almost
    # certainly been written for a different argument.
    with pytest.raises(InvalidShape, match="max_children"):
        FanOut(Zipf(), max_children=cast("Any", cap))


def test_the_cap_is_part_of_what_the_declaration_says() -> None:
    # The template-database cache keys on canonical(), so a capped declaration
    # served the uncapped world would be the cache answering for a world nobody
    # declared. One distribution shared between them, because two Zipf()
    # instances never compare equal and would make every pair here differ.
    sizes = Zipf()
    assert FanOut(sizes).canonical() == FanOut(sizes).canonical()
    assert FanOut(sizes, max_children=5).canonical() != FanOut(sizes).canonical()
    assert FanOut(sizes, max_children=5).canonical() != FanOut(sizes, max_children=6).canonical()


def test_the_cap_reads_back_in_the_repr_only_when_declared() -> None:
    assert "max_children=5" in repr(FanOut(Zipf(), max_children=5))
    assert "max_children" not in repr(FanOut(Zipf()))
