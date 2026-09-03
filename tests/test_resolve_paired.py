"""Reading the partner table, and the refusals only a resolved partition can make."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from django_data_shape import Constant, FanOut, InvalidShape, Paired, Uniform, Zipf
from django_data_shape.resolve_fan_out import resolve_fan_out
from django_data_shape.resolve_paired import resolve_paired
from tests.testapp.models import Company, Person


class _Cursor:
    def __init__(self, keys: list[int]) -> None:
        self._keys = keys

    def execute(self, sql: str, params: Any = None) -> None:
        self.sql = sql

    def fetchall(self) -> list[tuple[int]]:
        return [(key,) for key in self._keys]


class _Connection:
    """Everything the two resolvers touch, and nothing else."""

    def __init__(self, keys: list[int]) -> None:
        self._keys = keys
        self.ops = type("Ops", (), {"quote_name": staticmethod(lambda name: f'"{name}"')})()

    @contextmanager
    def cursor(self) -> Any:
        yield _Cursor(self._keys)


def _plan(rows: int, groups: int, sizes: Any = None) -> Any:
    return resolve_fan_out(
        FanOut(sizes or Zipf(1.2)),
        Company,
        rows,
        seed=7,
        table="testapp_membership",
        field="company",
        connection=_Connection(list(range(1, groups + 1))),
    )


def _resolve(rows: int, groups: int, partners: int, weights: Any = None, sizes: Any = None) -> Any:
    return resolve_paired(
        Paired("company", weights or Zipf(1.2)),
        Person,
        _plan(rows, groups, sizes),
        rows,
        seed=7,
        table="testapp_membership",
        field="person",
        connection=_Connection(list(range(100, 100 + partners))),
    )


def test_the_partner_keys_are_read_rather_than_assumed() -> None:
    # The same correction a fan-out carries: a project may have built the
    # partner table with the ORM, so its keys are whatever the sequence handed
    # out and a pairing pointing at 1..N would point at nothing.
    plan = _resolve(rows=20, groups=10, partners=8)

    assert all(plan.partner_for(0, i) >= 100 for i in range(plan.sizes()[0]))


def test_an_empty_partner_table_is_refused_by_name() -> None:
    with pytest.raises(InvalidShape, match="which has no rows"):
        _resolve(rows=20, groups=10, partners=0)


def test_an_empty_partner_table_is_fine_when_there_is_nothing_to_pair() -> None:
    # Zero rows is a legitimate declaration -- it is what an edge table nobody
    # has written to looks like -- so it must not be refused for being empty.
    assert _resolve(rows=0, groups=10, partners=0).sizes() == [0] * 10


def test_the_busiest_group_is_what_has_to_fit_and_the_refusal_says_so() -> None:
    """Not the row count against the product, which is far larger.

    Every row of one group needs a different partner, so what binds is the
    largest group against the partner count. A heavy tail puts a large share of
    every edge on one group, so a declaration that looks sparse -- twenty rows
    over ten groups and five partners, a product of fifty -- is still
    impossible, and only once the partition is resolved does anyone know it.
    """
    with pytest.raises(InvalidShape) as raised:
        _resolve(rows=20, groups=10, partners=5)

    message = str(raised.value)
    assert "distinct partners -- more than there are" in message
    assert "largest group against the partner count" in message


def test_a_weight_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(InvalidShape, match="needs numeric pairing weights"):
        _resolve(rows=20, groups=10, partners=40, weights=Constant("heavy"))


def test_weighing_every_partner_at_zero_is_refused() -> None:
    # Not a division by zero from inside the allocation: a declaration saying
    # no partner is ever chosen has no reading that builds.
    with pytest.raises(InvalidShape, match="weighs every"):
        _resolve(rows=20, groups=10, partners=40, weights=Constant(0))


def test_a_rounded_distribution_can_weigh_partners_too() -> None:
    # The same tower a fan-out's sizes accept, and for the same reason: a
    # rounded Uniform hands back a Decimal, which is a Number and deliberately
    # not a Real.
    assert _resolve(rows=20, groups=10, partners=40, weights=Uniform(1, 10, places=0)).sizes()
