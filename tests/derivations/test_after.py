"""A child's column landing some way past its parent's."""

from __future__ import annotations

import datetime

import pytest

from django_data_shape import After, InvalidShape, Scope

_SIGNED_UP = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)


def test_the_gap_starts_at_the_parents_value() -> None:
    after = After("account.signed_up_at", within=datetime.timedelta(days=10))

    assert after.value(row=0, draw=0.0, sources=(_SIGNED_UP,)) == _SIGNED_UP


def test_the_gap_spreads_across_within() -> None:
    after = After("account.signed_up_at", within=datetime.timedelta(days=10))

    assert after.value(row=0, draw=0.5, sources=(_SIGNED_UP,)) == _SIGNED_UP + datetime.timedelta(
        days=5
    )


def test_at_least_shifts_the_whole_window() -> None:
    after = After(
        "account.signed_up_at",
        within=datetime.timedelta(days=10),
        at_least=datetime.timedelta(days=1),
    )

    assert after.value(row=0, draw=0.0, sources=(_SIGNED_UP,)) == _SIGNED_UP + datetime.timedelta(
        days=1
    )


def test_it_works_in_whatever_unit_the_column_uses() -> None:
    # One class rather than a datetime one and a numeric one: the arithmetic is
    # the caller's types, and this only needs addition and a scale by a float.
    after = After("invoice.amount", within=100)

    assert after.value(row=0, draw=0.25, sources=(50,)) == 75.0


def test_it_reads_from_the_parent_row() -> None:
    assert After("account.signed_up_at", within=1).scope is Scope.PARENT
    assert After("account.signed_up_at", within=1).sources == ("account.signed_up_at",)


def test_a_negative_window_is_refused() -> None:
    with pytest.raises(InvalidShape, match="non-negative within"):
        After("account.signed_up_at", within=datetime.timedelta(days=-1))


def test_a_negative_floor_is_refused_because_it_would_mean_before() -> None:
    with pytest.raises(InvalidShape, match="non-negative at_least"):
        After("account.signed_up_at", within=1, at_least=-5)


def test_it_reads_back_as_what_was_declared() -> None:
    assert repr(After("a.b", within=10)) == "After('a.b', within=10, at_least=0)"
