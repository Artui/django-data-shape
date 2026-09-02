"""Computation order, which is not column order."""

from __future__ import annotations

import operator

import pytest

from django_data_shape import (
    After,
    Aligned,
    Constant,
    Derived,
    InvalidShape,
    Uniform,
)
from django_data_shape.order_derivations import order_derivations


def test_a_table_with_no_derivations_has_nothing_to_order() -> None:
    assert order_derivations("Order", {"status": Constant("x")}) == ()


def test_a_derivation_comes_after_the_derivation_it_reads() -> None:
    fields = {
        "total": Derived("unit_price", compute=operator.neg),
        "unit_price": Aligned("size", Uniform(1, 10)),
    }

    # Sorted by name these come out total-first, which is exactly the order that
    # would read an unfilled slot. The column order is for the COPY statement
    # and says nothing about what depends on what.
    assert order_derivations("Ticket", fields) == ("unit_price", "total")


def test_a_chain_is_ordered_all_the_way_down() -> None:
    fields = {
        "c": Derived("b", compute=str),
        "b": Derived("a", compute=str),
        "a": Aligned("size", Uniform(1, 10)),
    }

    assert order_derivations("Ticket", fields) == ("a", "b", "c")


def test_a_shared_dependency_is_computed_once() -> None:
    fields = {
        "a": Aligned("size", Uniform(1, 10)),
        "b": Derived("a", compute=str),
        "c": Derived("a", compute=str),
    }

    assert order_derivations("Ticket", fields) == ("a", "b", "c")


def test_a_source_that_is_not_derived_creates_no_edge() -> None:
    fields = {
        "total": Derived("quantity", compute=str),
        "quantity": Constant(3),
    }

    # A plain distribution is computed before every derivation regardless, so
    # naming one is not a dependency the ordering has to carry.
    assert order_derivations("Ticket", fields) == ("total",)


def test_a_parent_source_is_not_a_column_of_this_table() -> None:
    fields = {"opened_at": After("account.signed_up_at", within=1)}

    # "account.signed_up_at" names a column of another table, already loaded, so
    # it cannot create an edge here even though a column called "account" is
    # right there beside it.
    assert order_derivations("Ticket", fields) == ("opened_at",)


def test_a_cycle_is_refused_and_named() -> None:
    fields = {
        "a": Derived("b", compute=str),
        "b": Derived("a", compute=str),
    }

    with pytest.raises(InvalidShape, match="in a cycle: a -> b -> a"):
        order_derivations("Ticket", fields)


def test_a_derivation_reading_itself_is_refused_as_the_same_cycle() -> None:
    with pytest.raises(InvalidShape, match="a -> a"):
        order_derivations("Ticket", {"a": Derived("a", compute=str)})
