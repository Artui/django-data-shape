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
