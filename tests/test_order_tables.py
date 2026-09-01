"""Which table loads first, and why it has to be decided at all."""

from __future__ import annotations

import pytest

from django_data_shape import Constant, FanOut, InvalidShape, Table, Uniform, Zipf
from django_data_shape.order_tables import order_tables
from tests.testapp.models import Company, Left, Right, Session


def test_a_parent_is_ordered_before_its_child() -> None:
    child = Table(Session, rows=10, company=FanOut(Zipf()), label=Constant("s"))
    parent = Table(Company, rows=5, name=Constant("acme"))

    # Declared child-first, loaded parent-first. Not because the database
    # insists -- Django's foreign keys are deferred, so any order commits -- but
    # because a fan-out reads the parent's real keys.
    assert [t.model for t in order_tables((child, parent))] == [Company, Session]


def test_an_order_that_is_already_correct_is_left_alone() -> None:
    parent = Table(Company, rows=5, name=Constant("acme"))
    child = Table(Session, rows=10, company=FanOut(Zipf()), label=Constant("s"))

    assert [t.model for t in order_tables((parent, child))] == [Company, Session]


def test_a_parent_outside_the_shape_is_not_ordered_against() -> None:
    # The supported hybrid: the parent was built by the ORM because its row
    # count is small, and only the child is declared here. Its keys get read at
    # resolve time like any other parent's.
    child = Table(Session, rows=10, company=FanOut(Zipf()), label=Constant("s"))

    assert [t.model for t in order_tables((child,))] == [Session]


def test_two_tables_fanning_out_over_each_other_are_refused_by_name() -> None:
    left = Table(Left, rows=5, right=FanOut(Uniform(1, 2)))
    right = Table(Right, rows=5, left=FanOut(Uniform(1, 2)))

    with pytest.raises(InvalidShape, match="cycle") as raised:
        order_tables((left, right))

    message = str(raised.value)
    assert "Left" in message and "Right" in message
