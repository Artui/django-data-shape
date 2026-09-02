"""The mechanism itself: a caller's function over sources in a named scope."""

from __future__ import annotations

import operator

import pytest

from django_data_shape import Derivation, Derived, InvalidShape, Scope


def test_it_computes_from_the_sources_it_was_given() -> None:
    derived = Derived("quantity", "unit_price", compute=operator.mul)

    assert derived.value(row=0, draw=0.5, sources=(3, 7)) == 21


def test_the_sources_arrive_in_declaration_order() -> None:
    derived = Derived("a", "b", compute=lambda a, b: f"{a}-{b}")

    # Not sorted, unlike the column order: a function's arguments are
    # positional, so the one order the caller controls has to be the one they
    # wrote.
    assert derived.value(row=0, draw=0.0, sources=("first", "second")) == "first-second"


def test_the_default_scope_is_this_row() -> None:
    assert Derived("total", compute=str).scope is Scope.ROW


def test_a_scope_may_be_named_as_a_plain_string() -> None:
    # The declaration is data a reader scans, so "parent" has to work as well as
    # Scope.PARENT -- the same reason FanOut takes placement as a string.
    assert Derived("account.plan", compute=str, scope="parent").scope is Scope.PARENT


def test_an_unknown_scope_is_refused_and_the_options_listed() -> None:
    with pytest.raises(InvalidShape, match="row, parent, rank"):
        Derived("x", compute=str, scope="grandparent")


def test_a_derivation_with_no_sources_is_refused() -> None:
    # A column computed from nothing is a Constant, and saying it with a
    # callable hides that from the planner-facing half of the vocabulary.
    with pytest.raises(InvalidShape, match="at least one source"):
        Derived(compute=str)


def test_something_that_is_not_callable_is_refused() -> None:
    with pytest.raises(InvalidShape, match="must be callable"):
        Derived("x", compute="not a function")


def test_it_satisfies_the_derivation_protocol() -> None:
    # The protocol is what the resolver depends on; the faces are just
    # declarations over it, and a consumer's own class is as good as ours.
    assert isinstance(Derived("x", compute=str), Derivation)


def test_a_distribution_is_not_a_derivation() -> None:
    from django_data_shape import Constant

    # Both produce a value, and that is deliberately not enough: a distribution
    # is the marginal shape of a column and a derivation is not, so nothing that
    # enumerates distributions may pick one of these up by accident.
    assert not isinstance(Constant(1), Derivation)


def test_it_reads_back_as_what_was_declared() -> None:
    assert repr(Derived("a", "b", compute=operator.mul)) == (
        "Derived('a', 'b', compute='mul', scope='row')"
    )
