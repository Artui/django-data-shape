"""A many-to-many field's name is refused by name, not as a missing field.

``Wearer.awards`` is a column of nothing: its rows live in the through model, and
this package builds them the way it builds any table with two keys, by declaring
that model with ``FanOut`` and ``Paired``. The refusal used to say the model "has
no field named awards" and list its concrete fields, which sent the reader away
from the fix -- the field exists, and the list was the wrong place to look.
"""

from __future__ import annotations

import pytest

from django_data_shape import Constant, InvalidShape, Projection, Table
from tests.testapp.models import Wearer


def _refusal(raised: pytest.ExceptionInfo[InvalidShape]) -> str:
    return str(raised.value)


def _assert_names_the_fix(message: str, through: str, field: str) -> None:
    assert f"Wearer.{field} is a many-to-many field" in message
    assert f"through model {through}" in message
    assert "FanOut" in message
    assert "Paired" in message
    assert "has no field named" not in message


def test_a_table_field_naming_a_declared_through_model_points_at_it() -> None:
    with pytest.raises(InvalidShape) as raised:
        Table(Wearer, rows=3, name=Constant("w"), awards=Constant(1))

    _assert_names_the_fix(_refusal(raised), "Award", "awards")


def test_a_table_field_naming_an_auto_created_through_model_points_at_it() -> None:
    with pytest.raises(InvalidShape) as raised:
        Table(Wearer, rows=3, name=Constant("w"), badges=Constant(1))

    _assert_names_the_fix(_refusal(raised), "Wearer_badges", "badges")


def test_a_table_statistics_target_on_a_many_to_many_name_points_at_the_through_model() -> None:
    with pytest.raises(InvalidShape) as raised:
        Table(Wearer, rows=3, name=Constant("w"), statistics={"awards": 200})

    _assert_names_the_fix(_refusal(raised), "Award", "awards")


def test_a_table_unknown_name_beside_a_many_to_many_one_still_names_the_many_to_many_one() -> None:
    with pytest.raises(InvalidShape) as raised:
        Table(Wearer, rows=3, name=Constant("w"), nope=Constant(1), awards=Constant(1))

    _assert_names_the_fix(_refusal(raised), "Award", "awards")


def test_a_plain_unknown_name_keeps_the_concrete_field_list() -> None:
    with pytest.raises(InvalidShape, match="Wearer has no field named nope") as raised:
        Table(Wearer, rows=3, name=Constant("w"), nope=Constant(1))

    assert "Its concrete fields are: id, name" in _refusal(raised)


def test_a_projection_column_naming_a_many_to_many_field_points_at_the_through_model() -> None:
    with pytest.raises(InvalidShape) as raised:
        Projection(Wearer, columns=("id", "name", "awards"), sql="SELECT 1, 'x', 1")

    _assert_names_the_fix(_refusal(raised), "Award", "awards")


def test_a_projection_statistics_target_on_a_many_to_many_name_points_at_the_through_model() -> (
    None
):
    with pytest.raises(InvalidShape) as raised:
        Projection(
            Wearer,
            columns=("id", "name"),
            sql="SELECT 1, 'x'",
            statistics={"badges": 200},
        )

    _assert_names_the_fix(_refusal(raised), "Wearer_badges", "badges")
