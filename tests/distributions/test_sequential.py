from __future__ import annotations

import datetime

from django_data_shape import Sequential


def test_it_advances_with_the_row() -> None:
    sequential = Sequential(10, 3)

    assert [sequential.value(row, 0.5) for row in range(4)] == [10, 13, 16, 19]


def test_it_walks_a_timeline() -> None:
    start = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    sequential = Sequential(start, datetime.timedelta(seconds=3))

    assert sequential.value(0, 0.0) == start
    assert sequential.value(100, 0.0) == start + datetime.timedelta(seconds=300)


def test_the_draw_is_ignored() -> None:
    # Its value must not depend on the draw, or the planner would cost an index scan wrongly.
    sequential = Sequential(0, 1)

    assert sequential.value(5, 0.0) == sequential.value(5, 0.999)


def test_it_reads_back_as_what_was_declared() -> None:
    assert repr(Sequential(0, 1)) == "Sequential(0, 1)"


def test_it_says_whether_it_climbs_with_the_row() -> None:
    assert Sequential(0, 1).is_ascending() is True
    assert Sequential(0, -1).is_ascending() is False


def test_it_says_so_in_the_columns_own_units() -> None:
    # A date steps by a timedelta, so zero must come from the step, not a literal.
    forwards = Sequential(datetime.datetime(2020, 1, 1), datetime.timedelta(days=1))
    backwards = Sequential(datetime.datetime(2020, 1, 1), datetime.timedelta(days=-1))

    assert forwards.is_ascending() is True
    assert backwards.is_ascending() is False
