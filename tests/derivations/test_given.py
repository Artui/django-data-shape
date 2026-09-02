"""A distribution chosen by the parent's value."""

from __future__ import annotations

import pytest

from django_data_shape import Constant, Given, InvalidShape, Scope, Skew


def test_the_parents_value_picks_the_distribution() -> None:
    given = Given(
        "account.plan",
        {"free": Constant("low"), "enterprise": Constant("high")},
    )

    assert given.value(row=0, draw=0.5, sources=("free",)) == "low"
    assert given.value(row=0, draw=0.5, sources=("enterprise",)) == "high"


def test_the_chosen_distribution_still_gets_the_row_and_the_draw() -> None:
    given = Given("account.plan", {"free": Skew({"a": 1, "b": 1})})

    # A case is a real distribution, not a value: the conditional part is which
    # shape applies, and the shape still has to be drawn from.
    assert given.value(row=0, draw=0.1, sources=("free",)) == "a"
    assert given.value(row=0, draw=0.9, sources=("free",)) == "b"


def test_an_unlisted_value_falls_to_the_default() -> None:
    given = Given("account.plan", {"free": Constant("low")}, default=Constant("mid"))

    assert given.value(row=0, draw=0.5, sources=("trial",)) == "mid"


def test_an_unlisted_value_with_no_default_is_refused_and_names_both() -> None:
    given = Given("account.plan", {"free": Constant("low")})

    # The one refusal here that cannot happen at declaration time, because the
    # parent's values live in the parent table rather than in the declaration.
    with pytest.raises(InvalidShape, match="no case for 'trial'") as raised:
        given.value(row=0, draw=0.5, sources=("trial",))

    assert "account.plan" in str(raised.value)


def test_no_cases_at_all_is_refused() -> None:
    with pytest.raises(InvalidShape, match="at least one case"):
        Given("account.plan", {})


def test_it_reads_from_the_parent_row() -> None:
    given = Given("account.plan", {"free": Constant("low")})

    assert given.scope is Scope.PARENT
    assert given.sources == ("account.plan",)


def test_it_reads_back_as_what_was_declared() -> None:
    given = Given("account.plan", {"free": Constant("low")})

    assert repr(given) == "Given('account.plan', {'free': Constant('low')}, default=None)"
