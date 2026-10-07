"""What ``is_disjoint`` reads a key strategy as, and that ``build`` reads it so.

The predicate ``build`` and ``scaled_world`` both act on, so that a table one of
them builds beside existing rows is never one the other empties or refuses.
The world's half is held in ``tests/scaling``; the build's is held here, since
the build is where the predicate decides whether rows already in a table are
refused.
"""

from __future__ import annotations

import pytest

import django_data_shape
from django_data_shape import (
    Constant,
    KeyFunction,
    Md5Keys,
    SequentialKeys,
    Shape,
    Table,
    UuidKeys,
    build,
)
from django_data_shape.keys.is_disjoint import is_disjoint
from tests.testapp.models import Tenant


class _Answering:
    """A strategy implementing ``Disjoint`` and answering as told."""

    def __init__(self, disjoint: bool) -> None:
        self._disjoint = disjoint

    def key_for(self, row: int, stream: int) -> object:
        return UuidKeys().key_for(row, stream)

    def is_disjoint_from_existing_rows(self) -> bool:
        return self._disjoint


@pytest.mark.parametrize("keys", [UuidKeys(), Md5Keys(), _Answering(disjoint=True)])
def test_a_strategy_that_says_it_is_disjoint_is(keys: object) -> None:
    assert is_disjoint(keys) is True


def test_a_strategy_that_says_it_is_not_is_not() -> None:
    # Implementing the protocol is not the answer: the strategy is asked.
    assert is_disjoint(_Answering(disjoint=False)) is False


@pytest.mark.parametrize(
    "keys",
    [SequentialKeys(), KeyFunction(lambda row: f"page-{row}"), None],
    ids=["sequential", "key-function", "none"],
)
def test_a_strategy_that_does_not_implement_it_is_not(keys: object) -> None:
    # Read as not disjoint rather than asked, which is the safe direction: a
    # strategy that never said its keys cannot collide is treated as one whose
    # keys can.
    assert is_disjoint(keys) is False


def _over_a_callers_tenant(disjoint: bool) -> Shape:
    """A shape building tenants, over a table already holding one of the caller's."""
    Tenant.objects.create(name="caller")
    return Shape(Table(Tenant, rows=2, keys=_Answering(disjoint), name=Constant("built")), seed=5)


@pytest.mark.django_db
def test_build_builds_beside_rows_where_the_strategy_says_it_is_disjoint() -> None:
    build(_over_a_callers_tenant(disjoint=True), require_statistics=False)

    assert sorted(Tenant.objects.values_list("name", flat=True)) == ["built", "built", "caller"]


@pytest.mark.django_db
def test_build_refuses_rows_where_the_strategy_says_it_is_not() -> None:
    # Implementing the protocol does not exempt the table: the build asks, and
    # a strategy answering no is refused for the caller's row like one whose
    # keys can collide. Caught as Exception and checked by name, so this
    # collects on a tree where the build reads the protocol some other way.
    shape = _over_a_callers_tenant(disjoint=False)

    with pytest.raises(Exception) as refused:
        build(shape, require_statistics=False)

    assert refused.type is django_data_shape.ShapeNotEmpty
    assert list(Tenant.objects.values_list("name", flat=True)) == ["caller"]
