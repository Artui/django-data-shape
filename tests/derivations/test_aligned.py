"""Columns that are extreme in the same rows."""

from __future__ import annotations

from django_data_shape import Aligned, Scope, Uniform, Zipf


def test_the_shared_rank_replaces_this_columns_own_draw() -> None:
    aligned = Aligned("size", Uniform(0, 100))

    # The draw argument is ignored on purpose: a column's own stream is what
    # independence looks like, and independence is what is being declared away.
    assert aligned.value(row=0, draw=0.9, sources=(0.25,)) == 25.0


def test_two_columns_on_one_rank_agree_exactly() -> None:
    quantity = Aligned("size", Uniform(0, 10))
    price = Aligned("size", Uniform(100, 200))
    ranks = [0.1, 0.9, 0.5, 0.3]

    quantities = [quantity.value(row=i, draw=0.0, sources=(r,)) for i, r in enumerate(ranks)]
    prices = [price.value(row=i, draw=0.0, sources=(r,)) for i, r in enumerate(ranks)]

    # The whales are the same whales: not merely correlated, identically
    # ordered, because both read one draw.
    assert sorted(range(4), key=lambda i: quantities[i]) == sorted(
        range(4), key=lambda i: prices[i]
    )


def test_reverse_reads_the_same_rank_from_the_other_end() -> None:
    aligned = Aligned("size", Uniform(0, 100), reverse=True)

    assert aligned.value(row=0, draw=0.0, sources=(0.25,)) == 75.0


def test_reversing_the_lowest_rank_stays_inside_the_unit_interval() -> None:
    # 1 - 0.0 is exactly 1.0, which is outside the [0, 1) every distribution is
    # written against -- and Zipf raises ZeroDivisionError there rather than
    # returning something merely wrong. The clamp is why this returns a number.
    aligned = Aligned("size", Zipf(1.2), reverse=True)

    assert aligned.value(row=0, draw=0.0, sources=(0.0,)) > 0


def test_it_reads_a_shared_rank() -> None:
    aligned = Aligned("size", Uniform(0, 1))

    assert aligned.scope is Scope.RANK
    assert aligned.sources == ("size",)


def test_it_reads_back_as_what_was_declared() -> None:
    assert repr(Aligned("size", Zipf(1.2))) == "Aligned('size', Zipf(1.2), reverse=False)"
