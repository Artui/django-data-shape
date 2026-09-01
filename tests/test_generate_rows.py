"""Rows, not model instances."""

from __future__ import annotations

from django_data_shape import Constant, Sequential, Skew, Table
from django_data_shape.generate_rows import generate_rows
from tests.testapp.models import Company, Order


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
