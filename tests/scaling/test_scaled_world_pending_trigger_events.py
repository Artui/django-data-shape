"""A scaled world can be built over child rows the caller wrote in the same transaction.

Django creates PostgreSQL foreign keys ``DEFERRABLE INITIALLY DEFERRED``, so a
child row written earlier in a transaction leaves its foreign-key check queued
until commit -- and PostgreSQL refuses to ``TRUNCATE`` a table with checks still
queued against it. An insert into a parent alone queues nothing, which is why a
suite that only ever wrote parents before entering a world never met it; the
usual way in is a factory's ``SubFactory`` in the test's own setup.

PostgreSQL only, because the failure is PostgreSQL's: off it the declared tables
are emptied by ``DELETE``, which has no such refusal and is untouched by the fix.
"""

from __future__ import annotations

import contextlib

import pytest
from django.db import IntegrityError, connection, transaction

from django_data_shape import Constant, FanOut, Shape, Table, Zipf, scaled_world
from tests.testapp.models import Company, Session

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="pending trigger events are a PostgreSQL refusal of TRUNCATE",
    ),
]


def _company_and_sessions() -> Shape:
    return Shape(
        Table(Company, rows=4, name=Constant("world")),
        Table(Session, rows=8, label=Constant("world"), company=FanOut(Zipf())),
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


def test_nor_does_a_child_only_the_cascade_reaches() -> None:
    # The shape declares the parent alone, so the child is emptied only because
    # TRUNCATE ... CASCADE follows its foreign key -- and PostgreSQL refuses the
    # cascaded table for its pending checks exactly as it refuses a named one.
    # It is also the consequence the docstring states: inside the block the
    # caller does not see rows in an undeclared table that references a
    # declared one, and they are back afterwards.
    company, session = _caller_writes_a_parent_and_a_child()

    with scaled_world(Shape(Table(Company, rows=3, name=Constant("world"))), 1):
        assert Company.objects.count() == 3
        assert not Session.objects.exists()

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

    with scaled_world(_company_and_sessions(), 1):
        ...

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
