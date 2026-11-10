from __future__ import annotations

import datetime
import operator
from decimal import Decimal

import pytest
from django.db import connection

from django_data_shape import (
    After,
    Aligned,
    DerivationQueriedDatabase,
    Derived,
    FanOut,
    Given,
    Sequential,
    Shape,
    Skew,
    Table,
    Uniform,
    Zipf,
)
from django_data_shape import build as build_shape
from tests.testapp.models import Account, Supply, Ticket, Vendor

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="COPY loading and planner statistics need PostgreSQL",
    ),
]

_SIGNED_UP = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
_WINDOW = datetime.timedelta(days=30)


def _accounts(rows: int = 20) -> Table:
    return Table(
        Account,
        rows=rows,
        signed_up_at=Sequential(_SIGNED_UP, datetime.timedelta(days=7)),
        plan=Skew({"free": 0.5, "enterprise": 0.5}),
    )


def _tickets(rows: int = 400, **overrides: object) -> Table:
    fields: dict[str, object] = {
        "account": FanOut(Zipf(1.2)),
        "opened_at": After("account.signed_up_at", within=_WINDOW),
        "severity": Given(
            "account.plan",
            {"free": Skew({"low": 1}), "enterprise": Skew({"high": 1})},
        ),
        "quantity": Aligned("size", Uniform(1, 100, places=0)),
        "unit_price": Aligned("size", Uniform(1, 500, places=2)),
        "total": Derived("quantity", "unit_price", compute=operator.mul),
    }
    fields.update(overrides)
    return Table(Ticket, rows=rows, fields=fields)


def _built(**overrides: object) -> list[Ticket]:
    build_shape(Shape(_accounts(), _tickets(**overrides), seed=11))
    return list(Ticket.objects.select_related("account").all())


def test_every_child_lands_after_its_own_parent() -> None:
    tickets = _built()

    # After its own account, not the earliest: that is what correlation means here.
    assert tickets
    assert all(
        ticket.account.signed_up_at <= ticket.opened_at < ticket.account.signed_up_at + _WINDOW
        for ticket in tickets
    )


def test_the_conditional_skew_follows_the_parent_row() -> None:
    tickets = _built()

    plans = {ticket.severity for ticket in tickets if ticket.account.plan == "free"}
    assert plans == {"low"}
    assert {ticket.severity for ticket in tickets if ticket.account.plan == "enterprise"} == {
        "high"
    }


def test_the_within_row_derivation_holds_in_the_loaded_table() -> None:
    tickets = _built()

    # The column the application reads, so this also shows a Decimal survived COPY intact.
    assert all(ticket.total == ticket.quantity * ticket.unit_price for ticket in tickets)


def test_the_aligned_columns_are_large_in_the_same_rows() -> None:
    tickets = _built()
    quantities = [float(ticket.quantity) for ticket in tickets]
    prices = [float(ticket.unit_price) for ticket in tickets]

    assert _correlation(quantities, prices) > 0.99


def test_independent_columns_are_not_aligned_by_accident() -> None:
    # Independent columns correlate at nothing, so a passing alignment is about the rank.
    tickets = _built(
        quantity=Uniform(1, 100, places=0),
        unit_price=Uniform(1, 500, places=2),
    )
    quantities = [float(ticket.quantity) for ticket in tickets]
    prices = [float(ticket.unit_price) for ticket in tickets]

    assert abs(_correlation(quantities, prices)) < 0.2


def test_the_parents_values_are_read_rather_than_recomputed() -> None:
    # The same correction the fan-out took for keys, applied to values.
    for index in range(10):
        Account.objects.create(
            signed_up_at=_SIGNED_UP + datetime.timedelta(days=90 * index),
            plan="free" if index % 2 else "enterprise",
        )

    build_shape(Shape(_tickets(rows=200), seed=4))

    tickets = Ticket.objects.select_related("account").all()
    assert all(
        ticket.account.signed_up_at <= ticket.opened_at < ticket.account.signed_up_at + _WINDOW
        for ticket in tickets
    )
    assert all((ticket.severity == "low") == (ticket.account.plan == "free") for ticket in tickets)


def test_a_derivation_that_queries_the_database_is_refused() -> None:
    def _count_accounts(quantity: object, unit_price: object) -> object:
        return Decimal(Account.objects.count())

    # A hook that may query is a hook that will, and then nothing is COPY-loaded.
    with pytest.raises(DerivationQueriedDatabase, match="may not call the database"):
        build_shape(
            Shape(
                _accounts(),
                _tickets(total=Derived("quantity", "unit_price", compute=_count_accounts)),
                seed=11,
            )
        )


def test_the_refusal_names_the_table_and_its_derivations() -> None:
    def _query(quantity: object, unit_price: object) -> object:
        return Decimal(Account.objects.count())

    with pytest.raises(DerivationQueriedDatabase) as raised:
        build_shape(
            Shape(
                _accounts(),
                _tickets(total=Derived("quantity", "unit_price", compute=_query)),
                seed=11,
            )
        )

    message = str(raised.value)
    assert "Ticket" in message
    assert "total" in message


def _correlation(left: list[float], right: list[float]) -> float:
    """Pearson correlation, written out rather than pulled in."""
    count = len(left)
    mean_left = sum(left) / count
    mean_right = sum(right) / count
    covariance = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right, strict=True))
    spread_left = sum((a - mean_left) ** 2 for a in left) ** 0.5
    spread_right = sum((b - mean_right) ** 2 for b in right) ** 0.5
    return covariance / (spread_left * spread_right)


def test_a_parent_hidden_by_its_own_default_manager_is_still_a_parent() -> None:
    for index in range(4):
        Vendor.objects.create(name=f"v{index}", retired=index % 2 == 1)

    build_shape(
        Shape(
            Table(
                Supply,
                rows=200,
                vendor=FanOut(Uniform(1, 2)),
                vendor_name=Derived("vendor.name", compute=str, scope="parent"),
            ),
            seed=2,
        )
    )

    supplies = list(Supply.objects.select_related("vendor").all())
    # Keys and values must come from the same rows, or children point at a subset.
    assert len({supply.vendor_id for supply in supplies}) == 4
    assert all(supply.vendor_name == supply.vendor.name for supply in supplies)


def test_a_narrowed_fan_out_derives_from_the_parents_it_named() -> None:
    """``parents=`` and a parent-scoped derivation take the ORM route together."""
    vendors = [Vendor.objects.create(name=f"v{index}") for index in range(4)]
    named = [vendors[1].pk, vendors[3].pk]

    build_shape(
        Shape(
            Table(
                Supply,
                rows=200,
                vendor=FanOut(Uniform(1, 2), parents=named),
                vendor_name=Derived("vendor.name", compute=str, scope="parent"),
            ),
            seed=2,
        )
    )

    supplies = list(Supply.objects.select_related("vendor").all())
    assert {supply.vendor_id for supply in supplies} == set(named)
    assert all(supply.vendor_name == supply.vendor.name for supply in supplies)
