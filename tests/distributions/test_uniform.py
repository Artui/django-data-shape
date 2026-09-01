"""The least interesting distribution, named plainly on purpose."""

from __future__ import annotations

from decimal import Decimal

import pytest

from django_data_shape import InvalidShape, Uniform


def test_it_spreads_across_the_range() -> None:
    uniform = Uniform(0, 100)

    assert uniform.value(0, 0.0) == 0
    assert uniform.value(0, 0.5) == 50
    assert uniform.value(0, 0.25) == 25


def test_places_rounds_to_an_exact_decimal() -> None:
    uniform = Uniform(0, 500, places=2)
    value = uniform.value(0, 1 / 3)

    # Decimal rather than float: a numeric(10, 2) column rejects binary float
    # noise, and COPY hands the value over as text.
    assert isinstance(value, Decimal)
    assert value == Decimal("166.67")


def test_an_empty_or_inverted_range_is_refused() -> None:
    with pytest.raises(InvalidShape, match="high greater than low"):
        Uniform(100, 100)
    with pytest.raises(InvalidShape, match="high greater than low"):
        Uniform(100, 0)


def test_negative_places_is_refused() -> None:
    with pytest.raises(InvalidShape, match="places cannot be negative"):
        Uniform(0, 1, places=-1)


def test_it_reads_back_as_what_was_declared() -> None:
    assert repr(Uniform(0, 5)) == "Uniform(0, 5)"
    assert repr(Uniform(0, 5, places=2)) == "Uniform(0, 5, places=2)"


def test_nan_or_infinite_bounds_are_refused() -> None:
    # Same shape of bug as Skew's: ``high <= low`` is False for NaN, so the
    # range was accepted and then returned NaN for every row.
    with pytest.raises(InvalidShape, match="finite bounds"):
        Uniform(0, float("inf"))
    with pytest.raises(InvalidShape, match="finite bounds"):
        Uniform(float("nan"), 1)


def test_places_zero_fills_an_integer_column() -> None:
    assert Uniform(0, 10, places=0).value(0, 0.55) == Decimal("6")


def test_a_range_wider_than_the_default_decimal_precision_still_rounds() -> None:
    # Python's default decimal context carries 28 significant digits and
    # rounding past it raises InvalidOperation -- which used to surface from
    # inside the COPY loop, on a numeric(30, 2) column that would have taken the
    # value happily.
    assert Uniform(0, 1e27, places=2).value(0, 0.5) == Decimal("500000000000000000000000000.00")
    assert Uniform(0, 1e40, places=2).value(0, 0.5) == Decimal("5" + "0" * 39 + ".00")


def test_rounding_follows_the_literal_a_reader_wrote() -> None:
    # repr() rather than the float itself: Decimal(2.675) takes the full binary
    # expansion and rounds to 2.67, where the literal 2.675 reads as 2.68.
    assert Uniform(2.675, 2.6750001, places=2).value(0, 0.0) == Decimal("2.68")
