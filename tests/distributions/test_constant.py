"""A column that never varies is a shape, not a missing declaration."""

from __future__ import annotations

from django_data_shape import Constant


def test_every_row_gets_the_same_value() -> None:
    constant = Constant("web")

    assert [constant.value(row, row / 10) for row in range(5)] == ["web"] * 5


def test_it_reads_back_as_what_was_declared() -> None:
    assert repr(Constant("web")) == "Constant('web')"
