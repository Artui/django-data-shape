"""The distribution the package exists for."""

from __future__ import annotations

import pytest

from django_data_shape import InvalidShape, Skew


def test_declared_proportions_are_what_comes_out() -> None:
    skew = Skew({"complete": 0.98, "pending": 0.015, "cancelled": 0.005})
    draws = [i / 10_000 for i in range(10_000)]

    counts = {value: 0 for value in ("complete", "pending", "cancelled")}
    for i, d in enumerate(draws):
        counts[str(skew.value(i, d))] += 1

    # Exact, not approximate: the draws sweep the unit interval evenly, so the
    # counts are the declared weights and any drift is a bug in the bounds.
    assert counts == {"complete": 9800, "pending": 150, "cancelled": 50}


def test_weights_need_not_sum_to_one() -> None:
    # Counts are the readable form for a reader who knows their data as
    # "roughly fifty of these for every one of those".
    skew = Skew({"a": 50, "b": 50})

    assert skew.value(0, 0.25) == "a"
    assert skew.value(0, 0.75) == "b"


def test_the_top_of_the_interval_lands_on_the_last_value() -> None:
    # Floating-point accumulation can leave the final bound a hair under 1.0,
    # and a draw above it must still produce a value rather than falling off
    # the end. Reaching for the boundary directly is the only way to see it.
    skew = Skew({"a": 1, "b": 1, "c": 1})

    assert skew.value(0, 1.0) == "c"


def test_an_empty_distribution_is_refused() -> None:
    with pytest.raises(InvalidShape, match="at least one value"):
        Skew({})


def test_a_value_that_never_occurs_is_refused_by_name() -> None:
    with pytest.raises(InvalidShape, match="'never'") as raised:
        Skew({"always": 1, "never": 0, "impossible": -1})

    # Both offenders named, because fixing one at a time is the slowest way to
    # learn there were two.
    assert "'impossible'" in str(raised.value)


def test_it_reads_back_as_what_was_declared() -> None:
    assert repr(Skew({"a": 1})) == "Skew({'a': 1})"


def test_a_nan_or_infinite_weight_is_refused() -> None:
    # NaN compares False to every ordering, so ``w <= 0`` let it through -- and
    # a NaN weight makes every cumulative bound NaN, so no draw ever matches and
    # every row falls through to the last value. The declared distribution came
    # out inverted, with nothing raised.
    with pytest.raises(InvalidShape, match="positive and finite"):
        Skew({"a": float("nan"), "b": 1})
    with pytest.raises(InvalidShape, match="positive and finite"):
        Skew({"a": float("inf"), "b": 1})
