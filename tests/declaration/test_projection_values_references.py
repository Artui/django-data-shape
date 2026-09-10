"""One projected expression naming another, so a relationship between two
measure columns is stated rather than encoded in coefficients that agree.

A projected table's measure columns are usually related to each other -- a
requested amount and an approved one, a quantity and a total, an amount and the
rate derived from it. Before this, an expression could not name another column in
the same ``values=``, so the relationship had to be restated from the columns
both were computed from, with coefficients chosen so that the rule happens to
hold. A reader had to do arithmetic to find the invariant and nothing rechecked
it when either expression was edited.

Everything here is arithmetic over constants, so it runs on both backends: the
substitution is text this package performs before any statement exists, and the
part that is not portable is what a caller writes inside an expression. The
realistic case -- a varied measure over UUID keys -- is in
``test_projection_values_uuid.py``, where it needs PostgreSQL.
"""

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
    # Filled in so a test that says nothing about it still declares a legal
    # shape: the column cannot be null, has no default and is not one Route
    # carries, so a projection would have nothing to put in it otherwise.
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
    # The whole substitution turns on this. `1 + 1` spliced into `x * 3` without
    # brackets is 4, and nothing downstream could tell the two apart afterwards.
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
    # Substitution rather than sharing, which is the cost this feature carries:
    # the database evaluates the referenced expression once per reference. It is
    # only harmless because a deterministic expression gives the same answer
    # each time, and stating that is the point of the test.
    build(
        _shape(
            requested_amount=SqlValue("4"),
            approved_amount=SqlValue("{values.requested_amount} + {values.requested_amount}"),
        ),
        require_statistics=False,
    )

    assert set(Shipment.objects.values_list("approved_amount", flat=True)) == {8}


def test_a_percent_inside_a_referenced_expression_is_escaped_exactly_once() -> None:
    # The ordering trap. `render` escapes `%` last, so resolution has to produce
    # the whole raw expression before it runs -- escaping first and substituting
    # after turns a referenced `%` into `%%%%` and the driver refuses it.
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
    # The available names, because the whole reason to refuse here rather than
    # let the database do it is to say what could have been written instead.
    assert "requested_amount" in message


def test_a_reference_to_a_copied_column_points_at_the_source_alias() -> None:
    # `label` is a real column of the projected table and is reachable -- just
    # not through values=, because it is copied. A refusal that only said "no
    # such value" would send a reader looking for a bug in their own spelling.
    with pytest.raises(InvalidShape) as caught:
        _shape(requested_amount=SqlValue("{values.label}"), approved_amount=SqlValue("1"))

    assert "{source}.label" in str(caught.value)


def test_a_cycle_between_two_expressions_is_refused_by_name() -> None:
    with pytest.raises(InvalidShape) as caught:
        _shape(
            requested_amount=SqlValue("{values.approved_amount} * 2"),
            approved_amount=SqlValue("{values.requested_amount} / 2"),
        )

    # The path starts at the alphabetically first name in the cycle, so two
    # runs over one declaration report it identically.
    assert "approved_amount -> requested_amount -> approved_amount" in str(caught.value)


def test_an_expression_naming_itself_is_refused() -> None:
    # A cycle of length one, and the shortest way to write a declaration that
    # would otherwise recurse until Python gave up somewhere unrelated.
    with pytest.raises(InvalidShape) as caught:
        _shape(
            requested_amount=SqlValue("{values.requested_amount} + 1"),
            approved_amount=SqlValue("1"),
        )

    assert "requested_amount -> requested_amount" in str(caught.value)


def test_the_digest_follows_the_expression_a_reference_resolves_to() -> None:
    # Two declarations that differ only inside the referenced expression are two
    # databases, and a template reused across them would serve the wrong rows.
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
    # The other side of the digest claim, and the reason resolution happens at
    # declaration rather than at build: the two spell one statement, produce one
    # set of rows, and would waste a template database each if they disagreed.
    referenced = _shape(
        requested_amount=SqlValue("1 + 1"),
        approved_amount=SqlValue("{values.requested_amount} * 3"),
    )
    written_out = _shape(
        requested_amount=SqlValue("1 + 1"),
        approved_amount=SqlValue("(1 + 1) * 3"),
    )

    assert shape_digest(referenced) == shape_digest(written_out)
