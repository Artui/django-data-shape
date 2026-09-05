"""The three derivations that exist so a shape can still be cached.

`Derived` takes a callable, and a callable cannot be honestly digested -- two
lambdas share a name, and identical bytecode returns something else when a
constant it reads is edited elsewhere. So a shape holding one is refused by
`template_database`, and a column as ordinary as `total = quantity * price`
excluded a whole declaration from the reuse that turns a forty-second build
into a hundred-millisecond clone.

These say the same three computations as data instead.
"""

from __future__ import annotations

import datetime as dt
import operator

import pytest

from django_data_shape import (
    Constant,
    Copied,
    Derived,
    FanOut,
    InvalidShape,
    Offset,
    Product,
    Scope,
    Sequential,
    Shape,
    Table,
    UnhashableShape,
    Zipf,
    build,
    shape_digest,
)
from tests.testapp.models import Account, Ticket

pytestmark = pytest.mark.django_db


def _shape(*, with_lambdas: bool) -> Shape:
    """The same world twice: once written with callables, once as data."""
    return Shape(
        Table(
            Account,
            rows=5,
            signed_up_at=Sequential(
                dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), dt.timedelta(days=1)
            ),
            plan=Constant("free"),
        ),
        Table(
            Ticket,
            rows=20,
            account=FanOut(Zipf()),
            opened_at=(
                Derived(
                    "account.signed_up_at",
                    compute=lambda at: at + dt.timedelta(days=2),
                    scope=Scope.PARENT,
                )
                if with_lambdas
                else Copied("account.signed_up_at")
            ),
            severity=Constant("low"),
            quantity=Constant(3),
            total=(
                Derived("quantity", "unit_price", compute=operator.mul)
                if with_lambdas
                else Product("quantity", "unit_price")
            ),
            # All three appear in the digested shape, so `canonical` is
            # exercised on each of them rather than on whichever one the
            # arithmetic happened to need.
            unit_price=(
                Derived("quantity", compute=lambda q: q + 1)
                if with_lambdas
                else Offset("quantity", by=1)
            ),
        ),
        seed=7,
    )


def test_a_shape_written_with_callables_cannot_be_digested() -> None:
    """The behaviour these exist to route around, unchanged."""
    with pytest.raises(UnhashableShape, match="Derived"):
        shape_digest(_shape(with_lambdas=True))


def test_the_same_shape_written_as_data_can_be() -> None:
    digest = shape_digest(_shape(with_lambdas=False))

    assert len(digest) == 32
    # Stable across calls, which is the property a cache key needs and the one
    # a callable cannot offer.
    assert shape_digest(_shape(with_lambdas=False)) == digest


def test_the_data_form_builds_the_same_rows_as_the_callable_form() -> None:
    """The claim worth testing: same declaration, same data, cacheable.

    Built rather than compared field by field, because the point is that the
    replacement is a replacement -- a reader swapping one for the other must
    get the database they already had.
    """
    build(_shape(with_lambdas=False), require_statistics=False)

    tickets = list(Ticket.objects.order_by("id").values("opened_at", "total", "account_id"))
    accounts = dict(Account.objects.values_list("id", "signed_up_at"))

    assert len(tickets) == 20
    for ticket in tickets:
        assert ticket["total"] == 12
        assert ticket["opened_at"] == accounts[ticket["account_id"]]


def test_offset_moves_a_column_of_the_same_row() -> None:
    """ROW scope, which is the whole reason this exists beside `After`.

    `After` is parent-scoped only, so "later than this column of this row" had
    no spelling that a digest could read.
    """
    build(
        Shape(
            Table(
                Account,
                rows=4,
                signed_up_at=Sequential(
                    dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), dt.timedelta(days=1)
                ),
                plan=Constant("free"),
            ),
            Table(
                Ticket,
                rows=4,
                account=FanOut(Zipf()),
                opened_at=Sequential(
                    dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc), dt.timedelta(hours=1)
                ),
                severity=Constant("low"),
                quantity=Constant(1),
                unit_price=Constant(2),
                total=Offset("unit_price", by=5),
            ),
        ),
        require_statistics=False,
    )

    assert set(Ticket.objects.values_list("total", flat=True)) == {7}


def test_offset_refuses_a_negative_gap() -> None:
    with pytest.raises(InvalidShape, match="non-negative"):
        Offset("opened_at", by=dt.timedelta(days=-1))


def test_each_one_reports_itself() -> None:
    """A repr a reader can paste back into a declaration."""
    assert repr(Product("a", "b")) == "Product('a', 'b')"
    assert repr(Copied("parent.x")) == "Copied('parent.x')"
    assert repr(Offset("at", by=dt.timedelta(days=1))) == (
        "Offset('at', by=datetime.timedelta(days=1))"
    )
