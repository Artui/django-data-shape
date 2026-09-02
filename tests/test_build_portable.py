"""Building where there is no COPY and no planner to convince.

Runs on both backends on purpose. The ``not_postgres`` alias is SQLite whichever
backend the default is, so the portable path is exercised by the Postgres job
that carries the coverage gate -- the same reason the refusal paths read a vendor
instead of needing a second database.
"""

from __future__ import annotations

import datetime
import decimal
import operator

import pytest
from django.db import connections

from django_data_shape import (
    After,
    Aligned,
    Constant,
    DerivationQueriedDatabase,
    Derived,
    FanOut,
    Given,
    Projection,
    Sequential,
    Shape,
    Skew,
    Table,
    Uniform,
    UnsupportedBackend,
    Zipf,
    build,
)
from tests.testapp.models import (
    Account,
    Company,
    Event,
    EventSession,
    Order,
    Session,
    Template,
    TemplateSession,
    Ticket,
)

pytestmark = pytest.mark.django_db(databases=["default", "not_postgres"])

_AWARE = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
_ALIAS = "not_postgres"
_TIGHT = datetime.timedelta(minutes=1)


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


def _accounts_and_tickets(rows: int = 100) -> Shape:
    return Shape(
        Table(
            Account,
            rows=10,
            signed_up_at=Sequential(_AWARE, datetime.timedelta(days=7)),
            plan=Skew({"free": 0.5, "enterprise": 0.5}),
        ),
        Table(
            Ticket,
            rows=rows,
            fields={
                "account": FanOut(Zipf(1.2)),
                # A one-minute window on purpose. Any mishandling of the
                # parent's own value is a timezone-sized error, and a window
                # measured in days swallows one: the assertion below would hold
                # for a value six hours out of place, which is exactly what a
                # raw cursor produces here.
                "opened_at": After("account.signed_up_at", within=_TIGHT),
                "severity": Given(
                    "account.plan",
                    {"free": Skew({"low": 1}), "enterprise": Skew({"high": 1})},
                ),
                "quantity": Aligned("size", Uniform(1, 100, places=0)),
                "unit_price": Aligned("size", Uniform(1, 500, places=2)),
                "total": Derived("quantity", "unit_price", compute=operator.mul),
            },
        ),
        seed=9,
    )


def test_a_parent_column_arrives_as_a_python_value_off_postgres() -> None:
    build(_accounts_and_tickets(), using=_ALIAS, require_statistics=False)

    tickets = Ticket.objects.using(_ALIAS).select_related("account").all()

    # SQLite has no date type, so a DateTimeField read through a raw cursor
    # comes back as a string and After would be adding a timedelta to it. The
    # parent's values go through the ORM for exactly this reason, and this is
    # the assertion that says so rather than the comment.
    assert tickets
    assert all(
        ticket.account.signed_up_at <= ticket.opened_at < ticket.account.signed_up_at + _TIGHT
        for ticket in tickets
    )


def test_the_guard_covers_the_portable_route_too() -> None:
    def _query(quantity: object, unit_price: object) -> object:
        return decimal.Decimal(Account.objects.using(_ALIAS).count())

    shape = Shape(
        Table(
            Account,
            rows=10,
            signed_up_at=Sequential(_AWARE, datetime.timedelta(days=7)),
            plan=Skew({"free": 1}),
        ),
        Table(
            Ticket,
            rows=100,
            fields={
                "account": FanOut(Zipf(1.2)),
                "opened_at": Constant(_AWARE),
                "severity": Constant("low"),
                "quantity": Constant(1),
                "unit_price": Constant("1.00"),
                "total": Derived("quantity", "unit_price", compute=_query),
            },
        ),
        seed=9,
    )

    # The two routes install the guard differently -- one for the whole COPY
    # loop, one per generated chunk -- so a wiring that only held on PostgreSQL
    # would leave the rule true where it is easy and false where it is not.
    with pytest.raises(DerivationQueriedDatabase, match="may not call the database"):
        build(shape, using=_ALIAS, require_statistics=False)


def test_a_projection_fills_its_table_off_postgres_too() -> None:
    # A projection is one statement in ordinary SQL -- an INSERT ... SELECT with
    # a window function -- so it is on the backend-neutral side of the line this
    # package draws. What it does not buy off PostgreSQL is the same thing
    # nothing else buys there: statistics, and therefore a plan worth reading.
    build(
        Shape(
            Projection(EventSession, per=Event, copying=TemplateSession),
            Table(Template, rows=4, name=Constant("t")),
            Table(
                TemplateSession,
                rows=12,
                template=FanOut(Uniform(1, 3)),
                title=Constant("s"),
                minutes=Sequential(15, 1),
            ),
            Table(Event, rows=9, template=FanOut(Uniform(1, 3)), name=Constant("e")),
            seed=2,
        ),
        using=_ALIAS,
        require_statistics=False,
    )

    sizes = {
        template_id: TemplateSession.objects.using(_ALIAS).filter(template_id=template_id).count()
        for template_id in Template.objects.using(_ALIAS).values_list("id", flat=True)
    }
    expected = sum(
        sizes[template_id]
        for template_id in Event.objects.using(_ALIAS).values_list("template_id", flat=True)
    )

    assert expected > 0
    assert EventSession.objects.using(_ALIAS).count() == expected
    assert sorted(EventSession.objects.using(_ALIAS).values_list("id", flat=True)) == list(
        range(1, expected + 1)
    )
