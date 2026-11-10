from __future__ import annotations

import contextlib

import pytest
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

import django_data_shape
from django_data_shape import Constant, FanOut, Shape, Table, Zipf, scaled_world
from tests.testapp.models import (
    Club,
    Company,
    Event,
    MemberFee,
    OptionalChild,
    Section,
    Session,
    Template,
    UuidSession,
)

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="pending trigger events are a PostgreSQL refusal of TRUNCATE",
    ),
]


def _company_and_sessions(
    company: dict[str, int] | None = None, session: dict[str, int] | None = None
) -> Shape:
    return Shape(
        Table(Company, rows=4, name=Constant("world"), statistics=company),
        Table(Session, rows=8, label=Constant("world"), company=FanOut(Zipf()), statistics=session),
        seed=3,
    )


def _caller_writes_a_parent_and_a_child() -> tuple[Company, Session]:
    """What a factory with a ``SubFactory`` does in a test's setup."""
    company = Company.objects.create(name="caller")
    return company, Session.objects.create(company=company, label="caller")


def test_a_child_the_caller_wrote_does_not_stop_a_world_declaring_it() -> None:
    company, session = _caller_writes_a_parent_and_a_child()

    with scaled_world(_company_and_sessions(), 1) as rows:
        assert rows == 12
        assert not Company.objects.filter(name="caller").exists()

    # The checks were fired, not discarded, and the emptying still rolls back with the block.
    assert list(Company.objects.values_list("pk", "name")) == [(company.pk, "caller")]
    assert list(Session.objects.values_list("pk", "company_id", "label")) == [
        (session.pk, company.pk, "caller")
    ]


def test_a_world_declaring_only_the_parent_refuses_the_callers_child() -> None:
    # The shape declares the parent alone, and the caller's child references it.
    company, session = _caller_writes_a_parent_and_a_child()

    with (
        pytest.raises(
            Exception, match=r"testapp_session\.company_id -> testapp_company"
        ) as refused,
        contextlib.ExitStack() as entering,
    ):
        entering.enter_context(
            scaled_world(Shape(Table(Company, rows=3, name=Constant("world"))), 1)
        )

    assert refused.type is django_data_shape.ShapeReferenced
    assert list(Company.objects.values_list("pk", "name")) == [(company.pk, "caller")]
    assert list(Session.objects.values_list("pk", "company_id")) == [(session.pk, company.pk)]


def test_the_constraint_mode_the_world_set_does_not_outlive_it() -> None:
    # The capture holds it there.
    with connection.cursor() as cursor:
        cursor.execute("CREATE TEMPORARY TABLE shape_parent (id integer PRIMARY KEY)")
        cursor.execute(
            "CREATE TEMPORARY TABLE shape_child_immediate (parent_id integer "
            "REFERENCES shape_parent DEFERRABLE INITIALLY IMMEDIATE)"
        )
        cursor.execute(
            "CREATE TEMPORARY TABLE shape_child_deferred (parent_id integer "
            "REFERENCES shape_parent DEFERRABLE INITIALLY DEFERRED)"
        )

    _caller_writes_a_parent_and_a_child()
    with CaptureQueriesContext(connection) as captured, scaled_world(_company_and_sessions(), 1):
        ...
    assert "SET CONSTRAINTS ALL DEFERRED" in [query["sql"] for query in captured]

    with connection.cursor() as cursor:
        with pytest.raises(IntegrityError), transaction.atomic():
            cursor.execute("INSERT INTO shape_child_immediate VALUES (1)")

        with pytest.raises(IntegrityError), transaction.atomic():
            cursor.execute("INSERT INTO shape_child_deferred VALUES (1)")
            cursor.execute("SELECT count(*) FROM shape_child_deferred")
            assert cursor.fetchone() == (1,)
            connection.check_constraints()


def test_a_violation_the_caller_wrote_surfaces_at_entry_under_its_name() -> None:
    # Deleting the row is enough: PostgreSQL skips a queued check whose row no longer exists.
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO testapp_session (company_id, label) VALUES (999999, 'orphan') RETURNING id"
        )
        (orphan,) = cursor.fetchone()
        constraints = connection.introspection.get_constraints(cursor, "testapp_session")
    (foreign_key,) = (name for name, info in constraints.items() if info["foreign_key"])

    try:
        with pytest.raises(IntegrityError, match=foreign_key), contextlib.ExitStack() as entering:
            entering.enter_context(scaled_world(_company_and_sessions(), 1))
    finally:
        Session.objects.filter(pk=orphan).delete()


def _onto_the_delete_route() -> None:
    """An undeclared row referencing a declared table through a null key."""
    OptionalChild.objects.create(company=None, label="caller")


def test_on_the_delete_route_an_orphan_in_a_declared_table_is_removed_unchecked() -> None:
    # The two routes differ here.
    Company.objects.create(name="caller")
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO testapp_session (company_id, label) VALUES (999999, 'orphan') RETURNING id"
        )
        (orphan,) = cursor.fetchone()
    _onto_the_delete_route()

    try:
        with (
            CaptureQueriesContext(connection) as captured,
            scaled_world(_company_and_sessions(), 1),
        ):
            assert not Session.objects.filter(label="orphan").exists()
        assert 'DELETE FROM "testapp_session"' in [query["sql"] for query in captured]
        assert list(Session.objects.filter(label="orphan").values_list("pk", flat=True)) == [orphan]
    finally:
        Session.objects.filter(pk=orphan).delete()


@pytest.mark.parametrize("route", ["truncate", "delete"])
def test_a_violation_outside_the_declaration_surfaces_at_entry_on_either_route(route: str) -> None:
    # A fee pointing at no section, in a table the shape does not declare.
    fee = MemberFee.objects.create(section_id=999999, amount=1)
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, "testapp_memberfee")
    (foreign_key,) = (name for name, info in constraints.items() if info["foreign_key"])
    Company.objects.create(name="caller")
    if route == "delete":
        _onto_the_delete_route()

    try:
        with (
            CaptureQueriesContext(connection) as captured,
            pytest.raises(IntegrityError, match=foreign_key),
            contextlib.ExitStack() as entering,
        ):
            entering.enter_context(scaled_world(_company_and_sessions(), 1))
        emptying = [query["sql"].split()[0] for query in captured]
        assert "TRUNCATE" not in emptying
        assert ("DELETE" in emptying) is (route == "delete")
    finally:
        MemberFee.objects.filter(pk=fee.pk).delete()


@pytest.mark.parametrize("route", ["truncate", "delete"])
def test_inside_the_block_every_deferrable_constraint_is_deferred(route: str) -> None:
    # The test above pins the restoring half; this pins what the caller's block runs under.
    with connection.cursor() as cursor:
        cursor.execute("CREATE TEMPORARY TABLE shape_parent (id integer PRIMARY KEY)")
        cursor.execute(
            "CREATE TEMPORARY TABLE shape_child_immediate (parent_id integer "
            "REFERENCES shape_parent DEFERRABLE INITIALLY IMMEDIATE)"
        )
    _caller_writes_a_parent_and_a_child()
    if route == "delete":
        _onto_the_delete_route()

    with (
        CaptureQueriesContext(connection) as captured,
        scaled_world(_company_and_sessions(), 1),
        connection.cursor() as cursor,
    ):
        cursor.execute("INSERT INTO shape_child_immediate VALUES (1)")

    emptying = [query["sql"].split()[0] for query in captured]
    assert ("TRUNCATE" in emptying) is (route == "truncate")
    assert ("DELETE" in emptying) is (route == "delete")
    with connection.cursor() as cursor, pytest.raises(IntegrityError), transaction.atomic():
        cursor.execute("INSERT INTO shape_child_immediate VALUES (1)")


def _attstattarget(table: str, column: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT attstattarget FROM pg_attribute WHERE attrelid = %s::regclass AND attname = %s",
            [table, column],
        )
        return cursor.fetchone()[0]


def test_statistics_on_the_parent_survive_the_delete_route() -> None:
    # The ALTER met a check queued by the world's own DELETE of a referenced row.
    company = Company.objects.create(name="caller")
    _onto_the_delete_route()

    with (
        CaptureQueriesContext(connection) as captured,
        scaled_world(_company_and_sessions(company={"name": 200}), 1) as rows,
    ):
        assert rows == 12
        assert _attstattarget("testapp_company", "name") == 200

    assert 'DELETE FROM "testapp_company"' in [query["sql"] for query in captured]
    assert list(Company.objects.values_list("pk", "name")) == [(company.pk, "caller")]


def test_statistics_on_the_child_survive_the_delete_route() -> None:
    # The caller's pending check on the session is what the DELETE leaves unfired.
    company, session = _caller_writes_a_parent_and_a_child()
    _onto_the_delete_route()

    with (
        CaptureQueriesContext(connection) as captured,
        scaled_world(_company_and_sessions(session={"label": 200}), 1) as rows,
    ):
        assert rows == 12
        assert _attstattarget("testapp_session", "label") == 200

    assert 'DELETE FROM "testapp_session"' in [query["sql"] for query in captured]
    assert list(Session.objects.values_list("pk", "company_id", "label")) == [
        (session.pk, company.pk, "caller")
    ]


def test_statistics_on_a_section_survive_a_world_over_the_callers_club() -> None:
    # The caller's club is referenced by the caller's section, so the fan-out is narrowed to it.
    club = Club.objects.create(name="caller")
    section = Section.objects.create(club=club, name="caller")
    shape = Shape(
        Table(
            Section,
            rows=3,
            name=Constant("world"),
            club=FanOut(Zipf(), parents=[club.pk]),
            statistics={"name": 200},
        ),
        seed=5,
    )

    with scaled_world(shape, 1) as rows:
        assert rows == 3
        assert list(Section.objects.values_list("club_id", "name")) == [(club.pk, "world")] * 3

    assert list(Section.objects.values_list("pk", "club_id", "name")) == [
        (section.pk, club.pk, "caller")
    ]


def test_statistics_on_a_disjoint_table_survive_the_callers_own_rows() -> None:
    # Nothing was emptied: the only statements naming the table are the build's.
    template = Template.objects.create(name="caller")
    event = Event.objects.create(template=template, name="caller")
    session = UuidSession.objects.create(event=event, title="caller")
    shape = Shape(
        Table(
            UuidSession,
            rows=3,
            event=FanOut(Zipf(), parents=[event.pk]),
            title=Constant("world"),
            statistics={"title": 200},
        ),
        seed=5,
    )

    with (
        CaptureQueriesContext(connection) as captured,
        scaled_world(shape, 1) as rows,
    ):
        assert rows == 3
        assert _attstattarget("testapp_uuidsession", "title") == 200
        assert sorted(UuidSession.objects.values_list("title", flat=True)) == [
            "caller",
            "world",
            "world",
            "world",
        ]

    assert not [
        query["sql"] for query in captured if query["sql"].startswith(("TRUNCATE", "DELETE"))
    ]
    assert list(UuidSession.objects.values_list("pk", "title")) == [(session.pk, "caller")]
