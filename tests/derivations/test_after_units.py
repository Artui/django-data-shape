"""``After`` writes the parent's value plus an offset, so the two columns have
to be the same kind of thing.

``date + timedelta`` is a ``date``. A ``DateTimeField`` child filled across an
edge whose parent column is a ``DateField`` was therefore filled with dates:
``COPY`` accepted them, they landed as naive midnights, and the only signal was
Django's own per-row ``RuntimeWarning`` in the middle of a build.
"""

from __future__ import annotations

import datetime as dt

import pytest

from django_data_shape import After, Constant, FanOut, InvalidShape, Sequential, Table, Zipf
from tests.testapp.models import Performance, Ticket


def test_a_date_parent_under_a_datetime_child_is_refused() -> None:
    with pytest.raises(InvalidShape) as excinfo:
        Table(
            Performance,
            rows=10,
            playhouse=FanOut(Zipf()),
            starts_at=After("playhouse.opened_on", within=dt.timedelta(days=1)),
            doors_on=Constant(dt.date(2026, 1, 1)),
        )

    message = str(excinfo.value)
    assert "Performance.starts_at" in message
    assert "playhouse.opened_on" in message
    # Both kinds named, because a reader who has to look them up has been given
    # an error that knows more than it says.
    assert "DateField" in message
    assert "DateTimeField" in message


def test_a_datetime_parent_under_a_date_child_is_refused() -> None:
    """The other direction truncates rather than misdating, and is still wrong."""
    with pytest.raises(InvalidShape, match="doors_on"):
        Table(
            Performance,
            rows=10,
            playhouse=FanOut(Zipf()),
            starts_at=Sequential(
                dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), dt.timedelta(hours=1)
            ),
            doors_on=After("playhouse.announced_at", within=dt.timedelta(days=1)),
        )


def test_matching_kinds_are_accepted() -> None:
    """A `DateField` after a `DateField` is exactly what the edge is for."""
    Table(
        Performance,
        rows=10,
        playhouse=FanOut(Zipf()),
        starts_at=Sequential(
            dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), dt.timedelta(hours=1)
        ),
        doors_on=After("playhouse.opened_on", within=dt.timedelta(days=1)),
    )


def test_the_documented_datetime_case_still_builds() -> None:
    """The example in `After`'s own docstring, unchanged."""
    Table(
        Ticket,
        rows=10,
        account=FanOut(Zipf()),
        opened_at=After("account.signed_up_at", within=dt.timedelta(days=365)),
        severity=Constant("low"),
        quantity=Constant(1),
        unit_price=Constant("1.00"),
        total=Constant("1.00"),
    )
