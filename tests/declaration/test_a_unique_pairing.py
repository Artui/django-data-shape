from __future__ import annotations

from typing import Any

import pytest
from django.db import connection

from django_data_shape import Constant, FanOut, Shape, Zipf
from tests.declaration.test_a_relation_in_a_condition import (
    _assert_loads,
    _Case,
    _model,
    _paired,
    _refusal,
    _tables,
)

_ON_POSTGRES = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="the uniqueness is enforced by a PostgreSQL index under COPY",
)
_TWENTY = tuple(range(20))
_SPELLINGS = ("unique", "one_to_one", "constraint")
_OVER_MANY = {"company": FanOut(Zipf()), "owner": _paired()}
_OVER_ONE = {"company": FanOut(Zipf(), parents=(1,)), "owner": _paired()}


def _spelled(spelling: str, name: str, columns: dict[str, Any], **sizes: Any) -> _Case:
    if spelling == "constraint":
        return _Case(f"{name}_constraint", ("owner",), None, columns, people=300, **sizes)
    return _Case(
        f"{name}_{spelling}", (), None, columns, people=300, owner_unique=spelling, **sizes
    )


def _remedies(case: _Case) -> str:
    return _refusal(case).partition("What keeps it: ")[2].replace(case.name, "<model>")


# Each loads 20 of 20: one parent is one group, whose partners are all distinct.
@_ON_POSTGRES
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("spelling", _SPELLINGS)
@pytest.mark.parametrize(
    ("over", "columns", "companies"),
    [("parents", _OVER_ONE, 50), ("declared", _OVER_MANY, 1)],
)
def test_a_unique_pairing_over_one_parent_loads_under_every_spelling(
    spelling: str, over: str, columns: dict[str, Any], companies: int
) -> None:
    _assert_loads(_spelled(spelling, f"paired_one_{over}", columns, companies=companies), _TWENTY)


@pytest.mark.parametrize("spelling", ("unique", "one_to_one"))
def test_a_unique_pairing_over_many_parents_is_refused_as_its_constraint_is(spelling: str) -> None:
    message = _refusal(_spelled(spelling, "paired_many", _OVER_MANY))

    assert f"paired_many_{spelling}.owner is unique, so no two rows may share a partner" in message
    assert "which the column does not name" in message
    assert "Sequential" not in message
    assert _remedies(_spelled(spelling, "paired_many", _OVER_MANY)) == _remedies(
        _spelled("constraint", "paired_many", _OVER_MANY)
    )


@pytest.mark.parametrize("spelling", _SPELLINGS)
@pytest.mark.parametrize(
    ("sizes", "offered", "withheld"),
    [
        pytest.param(
            {},
            ("owner=FanOut(Constant(1)), or a FanOut with max_children=1", "owner left out"),
            ("Distinct",),
            id="room",
        ),
        pytest.param(
            {"people": 150},
            ("owner left out",),
            ("FanOut(Constant(1))", "max_children=1", "Distinct"),
            id="short_of_partners",
        ),
        pytest.param(
            {"owner_required": True},
            ("owner=FanOut(Constant(1)), or a FanOut with max_children=1",),
            ("left out", "Distinct"),
            id="required",
        ),
    ],
)
def test_a_unique_pairing_is_offered_the_same_remedies_under_every_spelling(
    spelling: str, sizes: dict[str, Any], offered: tuple[str, ...], withheld: tuple[str, ...]
) -> None:
    case = _spelled(spelling, "paired_offers", _OVER_MANY)._replace(**sizes)
    message = _refusal(case)

    for one in (*offered, "a Projection with columns= and sql="):
        assert one in message
    for one in withheld:
        assert one not in message


# Each offer, on the refused table's sizes, loads 20 of 20 under unique=True and one-to-one.
@_ON_POSTGRES
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("spelling", ("unique", "one_to_one"))
@pytest.mark.parametrize(
    ("offer", "columns"),
    [
        ("flat", {"company": FanOut(Zipf()), "owner": FanOut(Constant(1))}),
        ("cap", {"company": FanOut(Zipf()), "owner": FanOut(Zipf(), max_children=1)}),
        ("left_out", {"company": FanOut(Zipf())}),
    ],
)
def test_what_a_unique_pairing_is_offered_builds(
    spelling: str, offer: str, columns: dict[str, Any]
) -> None:
    _assert_loads(_spelled(spelling, f"paired_offer_{offer}", columns), _TWENTY)


@pytest.mark.parametrize("spelling", _SPELLINGS)
def test_a_unique_pairing_of_one_row_is_accepted(spelling: str) -> None:
    case = _spelled(spelling, "paired_one_row", _OVER_MANY, rows=1)
    Shape(*_tables(case, _model(case)))
