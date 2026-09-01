"""The fan-out declaration and its refusals."""

from __future__ import annotations

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
