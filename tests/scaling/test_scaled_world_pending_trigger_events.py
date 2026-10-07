"""A scaled world can be built over child rows the caller wrote in the same transaction.

Django creates PostgreSQL foreign keys ``DEFERRABLE INITIALLY DEFERRED``, so a
child row written earlier in a transaction leaves its foreign-key check queued
until commit -- and PostgreSQL refuses to ``TRUNCATE`` a table with checks still
queued against it. An insert into a parent alone queues nothing, which is why a
suite that only ever wrote parents before entering a world never met it; the
usual way in is a factory's ``SubFactory`` in the test's own setup.

The same queue meets the other emptying route later. ``DELETE`` runs with
checks pending, but every row it removes from a referenced table queues one of
its own, and PostgreSQL refuses ``ALTER TABLE`` on a table with checks still
queued -- which is the statement the build issues for a table declaring
``statistics=``. So the world fires them after its ``DELETE``s too.

PostgreSQL only, because the failure is PostgreSQL's: nothing else refuses a
statement for checks still queued.
"""

from __future__ import annotations

import contextlib

import pytest
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

import django_data_shape
from django_data_shape import Constant, FanOut, Shape, Table, Zipf, scaled_world
from tests.testapp.models import Club, Company, OptionalChild, Section, Session

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

    # Exactly the caller's rows, and nothing the world built: the checks were
    # fired, not discarded, and the emptying still rolled back with the block.
    assert list(Company.objects.values_list("pk", "name")) == [(company.pk, "caller")]
    assert list(Session.objects.values_list("pk", "company_id", "label")) == [
        (session.pk, company.pk, "caller")
    ]


def test_a_world_declaring_only_the_parent_refuses_the_callers_child() -> None:
    # The shape declares the parent alone, and the caller's child references
    # it. Emptying the parent used to empty the child too, through TRUNCATE ...
    # CASCADE, so the block ran without rows the caller had made in a table the
    # shape never named. A world now removes the rows of its declared tables and
    # nothing else, so it refuses instead, naming the reference -- before any
    # statement that would meet the child's pending check.
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
    # Firing the checks is SET CONSTRAINTS ALL IMMEDIATE and then ALL DEFERRED,
    # and the second of those, left in force, would defer every deferrable
    # constraint in the caller's transaction -- including one declared
    # INITIALLY IMMEDIATE, which would then accept a bad row it should refuse at
    # once. What restores the caller's mode is the rollback of the world's own
    # savepoint, so this pins that the statements stay inside it.
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

    # A world over empty tables fires nothing, so the caller writes rows first:
    # the checks are fired only on the way to a TRUNCATE, and this test is
    # about that route. The capture holds it there.
    _caller_writes_a_parent_and_a_child()
    with CaptureQueriesContext(connection) as captured, scaled_world(_company_and_sessions(), 1):
        ...
    assert "SET CONSTRAINTS ALL DEFERRED" in [query["sql"] for query in captured]

    with connection.cursor() as cursor:
        with pytest.raises(IntegrityError), transaction.atomic():
            cursor.execute("INSERT INTO shape_child_immediate VALUES (1)")

        # And the opposite direction: a world that left ALL IMMEDIATE behind
        # would refuse this insert on the spot. Inside its own savepoint, so the
        # bad row and its queued check are gone before pytest-django's teardown
        # runs check_constraints over the whole transaction.
        with pytest.raises(IntegrityError), transaction.atomic():
            cursor.execute("INSERT INTO shape_child_deferred VALUES (1)")
            cursor.execute("SELECT count(*) FROM shape_child_deferred")
            assert cursor.fetchone() == (1,)
            connection.check_constraints()


def test_a_violation_the_caller_wrote_surfaces_at_entry_under_its_name() -> None:
    # Firing the pending checks changes when they run, not whether: an orphan
    # the caller wrote used to fail at the end of the test, if at all, and now
    # fails on the way into the world, naming the constraint it breaks.
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO testapp_session (company_id, label) VALUES (999999, 'orphan') RETURNING id"
        )
        (orphan,) = cursor.fetchone()
        constraints = connection.introspection.get_constraints(cursor, "testapp_session")
    (foreign_key,) = (name for name, info in constraints.items() if info["foreign_key"])

    try:
        # Entered through an ExitStack rather than a with body, because the
        # world raises on the way in and a body would be a line that never runs.
        with pytest.raises(IntegrityError, match=foreign_key), contextlib.ExitStack() as entering:
            entering.enter_context(scaled_world(_company_and_sessions(), 1))
    finally:
        # pytest-django's teardown runs check_constraints too, and the orphan's
        # queued check would fail the test there. Deleting the row is enough:
        # PostgreSQL skips a queued check whose row no longer exists.
        Session.objects.filter(pk=orphan).delete()


def _onto_the_delete_route() -> None:
    """An undeclared row referencing a declared table through a null key.

    It holds a row in a table that references the companies, so a ``TRUNCATE``
    of them would have to take it along, and the world empties by ``DELETE``
    instead -- with nothing to refuse, because a null key references nothing.
    """
    OptionalChild.objects.create(company=None, label="caller")


def _attstattarget(table: str, column: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT attstattarget FROM pg_attribute WHERE attrelid = %s::regclass AND attname = %s",
            [table, column],
        )
        return cursor.fetchone()[0]


def test_statistics_on_the_parent_survive_the_delete_route() -> None:
    # The caller wrote a parent and no child, so nothing of the caller's is queued
    # against the companies: what the build's ALTER TABLE met was the check the world's own
    # DELETE of a referenced row had queued.
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
    # Here the queue is the caller's: the session written in setup left its
    # check pending on testapp_session, which the DELETE removes the row of
    # and does not fire.
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
    # The reported case, once its Section declares statistics=: the caller's
    # club is referenced by the caller's section, so the world empties the
    # sections by DELETE, and the fan-out is narrowed to the caller's club.
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
