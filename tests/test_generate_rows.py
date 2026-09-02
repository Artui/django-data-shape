"""Rows, not model instances."""

from __future__ import annotations

import datetime
import operator
from typing import Any, cast

from django_data_shape import (
    After,
    Aligned,
    Constant,
    Derived,
    FanOut,
    Given,
    Sequential,
    Skew,
    Table,
    Uniform,
)
from django_data_shape.fan_out_plan import FanOutPlan
from django_data_shape.generate_rows import generate_rows
from tests.testapp.models import Company, Order, Ticket

_SIGNED_UP = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)


def _order(rows: int) -> Table:
    return Table(
        Order,
        rows=rows,
        status=Skew({"complete": 1}),
        total=Constant(1),
        created_at=Sequential(0, 1),
    )


def test_primary_keys_are_a_dense_range_from_one() -> None:
    keys = [row[0] for row in generate_rows(_order(5), seed=0)]

    # Dense and from 1 because this package assigns them: it is what will let a
    # child's foreign key be satisfied without a lookup, and what makes a
    # self-referential tree acyclic by construction.
    assert keys == [1, 2, 3, 4, 5]


def test_columns_follow_the_stable_column_order() -> None:
    first = next(iter(generate_rows(_order(1), seed=0)))

    # (pk, channel, created_at, status, total) -- the sorted order columns()
    # promises, with the model's own default filled in for channel.
    assert first == (1, "web", 0, "complete", 1)


def test_zero_rows_generates_nothing() -> None:
    assert list(generate_rows(_order(0), seed=0)) == []


def test_it_streams_rather_than_materialising() -> None:
    # A million tuples is real memory and psycopg writes them one at a time, so
    # building the list first would buy nothing but peak RSS.
    rows = generate_rows(_order(1_000_000), seed=0)

    assert next(iter(rows))[0] == 1


def test_the_same_seed_reproduces_the_same_rows() -> None:
    assert list(generate_rows(_order(50), seed=7)) == list(generate_rows(_order(50), seed=7))


def test_a_different_seed_produces_different_rows() -> None:
    table = Table(Company, rows=50, name=Skew({"a": 1, "b": 1, "c": 1}))

    assert list(generate_rows(table, seed=1)) != list(generate_rows(table, seed=2))


def _plan(rows: int) -> FanOutPlan:
    """Two parents, each owning half the child range, with their own columns.

    Built by hand rather than resolved, so the generator can be exercised
    without a connection -- which is the same property the fan-out partition has
    and the reason it is worth keeping.
    """
    return FanOutPlan(
        keys=[10, 20],
        starts=[0, rows // 2],
        rows=rows,
        null_stream=0,
        null_share=0.0,
        interleave=False,
        parent_values={
            "signed_up_at": [_SIGNED_UP, _SIGNED_UP + datetime.timedelta(days=100)],
            "plan": ["free", "enterprise"],
        },
    )


def _tickets(rows: int, **overrides: object) -> Table:
    fields: dict[str, object] = {
        "account": FanOut(Uniform(1, 2), placement="grouped"),
        "opened_at": After("account.signed_up_at", within=datetime.timedelta(days=10)),
        "severity": Given(
            "account.plan", {"free": Constant("low"), "enterprise": Constant("high")}
        ),
        "quantity": Aligned("size", Uniform(1, 100, places=0)),
        "unit_price": Aligned("size", Uniform(1, 500, places=2)),
        "total": Derived("quantity", "unit_price", compute=operator.mul),
    }
    fields.update(overrides)
    return Table(Ticket, rows=rows, fields=cast("Any", fields))


def _column(table: Table, rows: list[tuple[object, ...]], name: str) -> list[object]:
    slot = [column for column, _ in table.columns()].index(name)
    return [row[slot + 1] for row in rows]


def test_a_derivation_reads_another_column_of_the_same_row() -> None:
    table = Table(
        Order,
        rows=4,
        status=Skew({"complete": 1}),
        total=Constant(1),
        created_at=Sequential(0, 1),
        note=Derived("status", compute=str.upper),
    )

    rows = list(generate_rows(table, seed=0))

    assert _column(table, rows, "note") == ["COMPLETE"] * 4


def test_a_derived_column_is_computed_after_the_ones_it_reads() -> None:
    table = _tickets(8)

    rows = list(generate_rows(table, seed=3, plans={"account": _plan(8)}))

    # Sorted by name, total comes before unit_price, so a generator following
    # the column order would multiply by an unfilled slot. Every row agreeing is
    # what says the two orders are actually separate.
    quantities = _column(table, rows, "quantity")
    prices = _column(table, rows, "unit_price")
    assert _column(table, rows, "total") == [q * p for q, p in zip(quantities, prices, strict=True)]


def test_two_columns_on_one_rank_are_extreme_in_the_same_rows() -> None:
    # Unrounded on purpose: rounding to whole units puts two hundred rows into a
    # hundred buckets, and then an ordering test is comparing tie-breaks rather
    # than ranks.
    table = _tickets(
        200,
        quantity=Aligned("size", Uniform(1, 100)),
        unit_price=Aligned("size", Uniform(1, 500)),
    )

    rows = list(generate_rows(table, seed=3, plans={"account": _plan(200)}))
    quantities = _column(table, rows, "quantity")
    prices = _column(table, rows, "unit_price")

    # Both are monotonic in the shared draw, so the orderings are identical --
    # not merely correlated. Independent marginals give a database that is
    # realistic per column and unrealistic per entity.
    by_quantity = sorted(range(200), key=lambda i: (quantities[i], i))
    by_price = sorted(range(200), key=lambda i: (prices[i], i))
    assert by_quantity == by_price


def test_a_rank_is_not_the_column_of_the_same_name() -> None:
    ranked = Table(
        Order,
        rows=50,
        status=Skew({"complete": 1}),
        total=Aligned("total", Uniform(0, 100)),
        created_at=Sequential(0, 1),
    )
    unranked = Table(
        Order,
        rows=50,
        status=Skew({"complete": 1}),
        total=Uniform(0, 100),
        created_at=Sequential(0, 1),
    )

    # A rank called "total" must not silently be the "total" column's own draw,
    # or a declaration would align a column to itself and be told nothing.
    assert _column(ranked, list(generate_rows(ranked, seed=0)), "total") != _column(
        unranked, list(generate_rows(unranked, seed=0)), "total"
    )


def test_a_parent_scoped_derivation_reads_the_owning_parents_value() -> None:
    table = _tickets(4)

    rows = list(generate_rows(table, seed=1, plans={"account": _plan(4)}))

    # Grouped placement, two parents, four rows: the first two belong to the
    # first account and the last two to the second, which is what makes this
    # assertable without a database.
    assert _column(table, rows, "severity") == ["low", "low", "high", "high"]
    opened = _column(table, rows, "opened_at")
    assert all(_SIGNED_UP <= at < _SIGNED_UP + datetime.timedelta(days=10) for at in opened[:2])
    assert all(_SIGNED_UP + datetime.timedelta(days=100) <= at for at in opened[2:])
