from __future__ import annotations

import pytest

from django_data_shape import (
    Constant,
    FanOut,
    InvalidShape,
    Projection,
    Shape,
    Skew,
    SqlValue,
    Table,
    Uniform,
    Zipf,
    build,
    shape_digest,
)
from tests.testapp.models import Depot, Region, Route, Shipment

pytestmark = pytest.mark.django_db


def _shape(**values: SqlValue) -> Shape:
    # Keeps a test that says nothing about the column declaring a legal shape.
    values.setdefault("settled_amount", SqlValue("0"))
    return Shape(
        Table(Region, rows=3, name=Constant("r")),
        Table(Depot, rows=12, region=FanOut(Uniform(1, 4)), name=Constant("d")),
        Table(
            Route,
            rows=20,
            region=FanOut(Zipf(1.1)),
            label=Skew({"north": 0.5, "south": 0.5}),
        ),
        Projection(Shipment, per=Depot, copying=Route, values=values),
        seed=5,
    )


def test_a_reference_is_parenthesised_so_precedence_survives_it() -> None:
    # The whole substitution turns on this.
    build(
        _shape(
            requested_amount=SqlValue("1 + 1"),
            approved_amount=SqlValue("{values.requested_amount} * 3"),
        ),
        require_statistics=False,
    )

    assert set(Shipment.objects.values_list("approved_amount", flat=True)) == {6}


def test_references_chain_through_more_than_one_hop() -> None:
    build(
        _shape(
            requested_amount=SqlValue("2 * 3"),
            approved_amount=SqlValue("{values.requested_amount} + 1"),
            settled_amount=SqlValue("{values.approved_amount} * 2"),
        ),
        require_statistics=False,
    )

    assert set(Shipment.objects.values_list("settled_amount", flat=True)) == {14}


def test_one_expression_referenced_twice_is_written_out_twice() -> None:
    # Substitution, not sharing: the database evaluates the expression once per reference.
    build(
        _shape(
            requested_amount=SqlValue("4"),
            approved_amount=SqlValue("{values.requested_amount} + {values.requested_amount}"),
        ),
        require_statistics=False,
    )

    assert set(Shipment.objects.values_list("approved_amount", flat=True)) == {8}


def test_a_percent_inside_a_referenced_expression_is_escaped_exactly_once() -> None:
    # The ordering trap.
    build(
        _shape(
            requested_amount=SqlValue("17 % 5"),
            approved_amount=SqlValue("{values.requested_amount} + 10"),
        ),
        require_statistics=False,
    )

    assert set(Shipment.objects.values_list("approved_amount", flat=True)) == {12}


def test_a_reference_to_a_name_no_value_declares_is_refused() -> None:
    with pytest.raises(InvalidShape) as caught:
        _shape(
            requested_amount=SqlValue("1"),
            approved_amount=SqlValue("{values.requsted_amount} / 2"),
        )

    message = str(caught.value)
    assert "requsted_amount" in message
    # Lists the available names, so the reader learns what could have been written.
    assert "requested_amount" in message


def test_a_reference_to_a_copied_column_points_at_the_source_alias() -> None:
    # label is reachable, just not through values=, because it is copied.
    with pytest.raises(InvalidShape) as caught:
        _shape(requested_amount=SqlValue("{values.label}"), approved_amount=SqlValue("1"))

    assert "{source}.label" in str(caught.value)


def test_a_cycle_between_two_expressions_is_refused_by_name() -> None:
    with pytest.raises(InvalidShape) as caught:
        _shape(
            requested_amount=SqlValue("{values.approved_amount} * 2"),
            approved_amount=SqlValue("{values.requested_amount} / 2"),
        )

    # The path starts at the alphabetically first name, so runs report it identically.
    assert "approved_amount -> requested_amount -> approved_amount" in str(caught.value)


def test_an_expression_naming_itself_is_refused() -> None:
    # A cycle of length one, which would otherwise recurse until Python gave up.
    with pytest.raises(InvalidShape) as caught:
        _shape(
            requested_amount=SqlValue("{values.requested_amount} + 1"),
            approved_amount=SqlValue("1"),
        )

    assert "requested_amount -> requested_amount" in str(caught.value)


def test_the_digest_follows_the_expression_a_reference_resolves_to() -> None:
    # Declarations differing only inside the referenced expression are two databases.
    one = _shape(
        requested_amount=SqlValue("1 + 1"),
        approved_amount=SqlValue("{values.requested_amount} * 3"),
    )
    other = _shape(
        requested_amount=SqlValue("1 + 2"),
        approved_amount=SqlValue("{values.requested_amount} * 3"),
    )

    assert shape_digest(one) != shape_digest(other)


def test_a_reference_and_the_expression_written_out_are_one_database() -> None:
    # A reference and its written-out form produce one set of rows, so one database.
    referenced = _shape(
        requested_amount=SqlValue("1 + 1"),
        approved_amount=SqlValue("{values.requested_amount} * 3"),
    )
    written_out = _shape(
        requested_amount=SqlValue("1 + 1"),
        approved_amount=SqlValue("(1 + 1) * 3"),
    )

    assert shape_digest(referenced) == shape_digest(written_out)
