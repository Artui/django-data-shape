"""Building where there is no COPY and no planner to convince.

Runs on both backends on purpose. The ``not_postgres`` alias is SQLite whichever
backend the default is, so the portable path is exercised by the Postgres job
that carries the coverage gate -- the same reason the refusal paths read a vendor
instead of needing a second database.
"""

from __future__ import annotations

import datetime

import pytest
from django.db import connections

from django_data_shape import (
    Constant,
    FanOut,
    Shape,
    Skew,
    Table,
    UnsupportedBackend,
    Zipf,
    build,
)
from tests.testapp.models import Company, Order, Session

pytestmark = pytest.mark.django_db(databases=["default", "not_postgres"])

_AWARE = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
_ALIAS = "not_postgres"


def _orders(rows: int = 50) -> Shape:
    return Shape(
        Table(
            Order,
            rows=rows,
            status=Skew({"complete": 0.9, "pending": 0.1}),
            total=Constant("1.00"),
            created_at=Constant(_AWARE),
        ),
        seed=5,
    )


def test_the_default_still_refuses_a_backend_that_cannot_carry_a_plan() -> None:
    # The pair below is the whole design in two calls: the same shape, the same
    # connection, one keyword apart. Asking for statistics on a backend that has
    # none is refused exactly as before.
    with pytest.raises(UnsupportedBackend, match="needs PostgreSQL"):
        build(_orders(), using=_ALIAS)


def test_and_loads_the_rows_when_statistics_are_not_required() -> None:
    result = build(_orders(rows=50), using=_ALIAS, require_statistics=False)

    assert result.rows == 50
    assert Order.objects.using(_ALIAS).count() == 50


def test_the_declared_skew_is_what_lands_there_too() -> None:
    build(_orders(rows=1000), using=_ALIAS, require_statistics=False)

    pending = Order.objects.using(_ALIAS).filter(status="pending").count()

    # Cardinality is the half that is backend-neutral, and this is what that
    # sentence means in practice: the distribution is the declared one here, and
    # only the planner's opinion of it is missing.
    assert 60 < pending < 140


def test_a_relation_still_points_at_rows_that_exist() -> None:
    build(
        Shape(
            Table(Company, rows=20, name=Constant("acme")),
            Table(Session, rows=200, label=Constant("x"), company=FanOut(Zipf())),
            seed=5,
        ),
        using=_ALIAS,
        require_statistics=False,
    )

    parents = set(Company.objects.using(_ALIAS).values_list("pk", flat=True))
    children = set(Session.objects.using(_ALIAS).values_list("company_id", flat=True))

    assert children <= parents
    assert Session.objects.using(_ALIAS).count() == 200


def test_nothing_is_analyzed_where_the_plan_would_mean_nothing() -> None:
    build(_orders(rows=50), using=_ALIAS, require_statistics=False)

    with connections[_ALIAS].cursor() as cursor:
        cursor.execute("SELECT count(*) FROM sqlite_master WHERE name = 'sqlite_stat1'")
        analyzed = cursor.fetchone()[0]

    # SQLite has an ANALYZE of its own and running it would be one line. It is
    # not run, and this is the assertion that keeps it that way: plan realism on
    # SQLite is out of scope, so a statistics table behind these rows would be
    # this package claiming something it has said it will not claim.
    assert analyzed == 0


def test_the_next_create_does_not_collide_with_the_keys_it_assigned() -> None:
    # No sequence to reset here -- Django's sequence_reset_sql is empty for
    # SQLite -- so this is the claim that the reset step is unnecessary rather
    # than merely skipped. Without it holding, the first ORM write in a
    # consumer's test would fail on a primary key that is already taken.
    build(_orders(rows=25), using=_ALIAS, require_statistics=False)

    created = Order.objects.using(_ALIAS).create(status="complete", total="1.00", created_at=_AWARE)

    assert created.pk > 25


def test_a_load_larger_than_one_chunk_lands_completely() -> None:
    # More rows than one chunk, so the loop runs more than once and the last,
    # partial one is counted too. It asserts the arithmetic of the loop and not
    # that chunking happened -- a path that handed the whole iterator to
    # executemany would land the same rows, and only a memory profile could tell
    # the two apart. The reason for chunking is written where it is done.
    result = build(_orders(rows=2500), using=_ALIAS, require_statistics=False)

    assert result.rows == 2500 == Order.objects.using(_ALIAS).count()
