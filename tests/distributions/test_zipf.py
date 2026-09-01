"""Heavy-tailed weights, which is what makes fan-out worth declaring."""

from __future__ import annotations

import pytest

from django_data_shape import InvalidShape, Zipf


def test_it_produces_a_head_and_a_long_tail() -> None:
    zipf = Zipf(1.2)
    weights = sorted((zipf.value(i, i / 1000) for i in range(1000)), reverse=True)
    total = sum(weights)

    # The property that matters is concentration: the top of the distribution
    # holds a disproportionate share, which is what makes the planner's
    # n_distinct average a lie for the head and for the tail alike.
    assert sum(weights[:10]) / total > 0.15
    assert sum(weights[500:]) / total < 0.30


def test_every_weight_is_positive_and_finite() -> None:
    zipf = Zipf(1.2)

    # draw is in [0, 1), so 1 - draw never reaches zero and the power stays
    # finite for every draw the generator can produce.
    assert all(0 < zipf.value(i, i / 4096) < float("inf") for i in range(4096))


def test_a_larger_exponent_is_a_lighter_tail() -> None:
    light = sorted((Zipf(3.0).value(i, i / 500) for i in range(500)), reverse=True)
    heavy = sorted((Zipf(1.05).value(i, i / 500) for i in range(500)), reverse=True)

    assert sum(heavy[:5]) / sum(heavy) > sum(light[:5]) / sum(light)


@pytest.mark.parametrize("s", [0, -1, float("nan"), float("inf")])
def test_a_non_positive_or_infinite_exponent_is_refused(s: float) -> None:
    with pytest.raises(InvalidShape, match="positive finite exponent"):
        Zipf(s)
