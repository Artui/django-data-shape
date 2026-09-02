"""A column that never varies is a shape, not a missing declaration."""

from __future__ import annotations

from django_data_shape import Constant


def test_every_row_gets_the_same_value() -> None:
    constant = Constant("web")

    assert [constant.value(row, row / 10) for row in range(5)] == ["web"] * 5


def test_it_reads_back_as_what_was_declared() -> None:
    assert repr(Constant("web")) == "Constant('web')"


def test_it_reports_a_single_distinct_value() -> None:
    # What makes a unique column with more than one row decidably impossible
    # at declaration time rather than partway through a load.
    assert Constant("web").distinct_values() == 1


def test_the_one_value_holds_all_of_the_rows() -> None:
    assert Constant("COMPLETE").shares() == {"COMPLETE": 1.0}
