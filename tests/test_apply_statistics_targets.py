"""Statistics targets: what the planner is asked to keep, and what it refuses to miss."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.db import connection

from django_data_shape import Constant, FanOut, InvalidShape, Projection, Shape, Skew, Table, Zipf
from django_data_shape import build as build_shape
from django_data_shape.apply_statistics_targets import apply_statistics_targets
from tests.testapp.models import (
    Bucketed,
    Event,
    Narrowed,
    TargetedSession,
    Template,
    TemplateSession,
)

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="statistics targets and the catalogue they live in need PostgreSQL",
    ),
]

# More distinct values than PostgreSQL's default target of a hundred, which is
# the whole case: a declaration this size is invisible to the planner unless it
# says so.
_WIDE = 150


def _wide_skew(values: int = _WIDE) -> Skew:
    # Descending weights rather than equal ones, so this is a skew rather than a
    # uniform categorical: the most-common-value list is what the assertions
    # below read, and a flat column is the one shape it may legitimately decline
    # to fill.
    return Skew({f"code-{index:03d}": float(values - index) for index in range(values)})


def _target(model: type, column: str) -> int:
    """The target PostgreSQL will actually use for one column, default included."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COALESCE(NULLIF(attstattarget, -1), "
            "current_setting('default_statistics_target')::int) "
            "FROM pg_attribute WHERE attrelid = %s::regclass AND attname = %s",
            [model._meta.db_table, column],
        )
        return int(cursor.fetchone()[0])


def _default_target() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('default_statistics_target')::int")
        return int(cursor.fetchone()[0])


def _sequence_value(model: type) -> int:
    """Where this table's identity sequence has been left.

    Read rather than assumed: ``setval`` is not transactional and a
    transactional test's truncation does not reset sequences, so the value at
    the start of a test is whatever the run has done to it so far.
    """
    with connection.cursor() as cursor:
        # In two steps because pg_get_serial_sequence returns the sequence's
        # name as text, and a name is not a relation until it is written into
        # the FROM clause.
        cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [model._meta.db_table])
        cursor.execute(f"SELECT last_value, is_called FROM {cursor.fetchone()[0]}")
        last_value, is_called = cursor.fetchone()
    return int(last_value) + int(is_called)


def _most_common(model: type, column: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT coalesce(array_length(most_common_vals, 1), 0) FROM pg_stats "
            "WHERE tablename = %s AND attname = %s",
            [model._meta.db_table, column],
        )
        row = cursor.fetchone()
    return 0 if row is None else int(row[0])


def _fresh(model: type, column: str) -> None:
    """Put one table back to the state a test is entitled to assume it is in.

    Nothing this module touches is rolled back between tests. A statistics
    target is DDL that commits with the build; ``pg_statistic`` rows survive the
    truncation a transactional test does at teardown; and a sequence moved by
    ``setval`` stays moved because ``setval`` is not transactional. So a test
    that merely asserted its precondition would be asserting whatever the test
    before it happened to leave -- and one that assumed a precondition would
    pass on another test's work.

    This establishes the state and then checks it, which is the only version of
    the two that survives a mutation to the code under test. Both halves have
    been earned here: a mutation moving the target to after the ``ANALYZE``
    passed every one of these assertions until this function existed, because
    the previous test had already set the target and moved the sequence.
    """
    table = model._meta.db_table
    with connection.cursor() as cursor:
        # -1 is PostgreSQL's own spelling of "back to default_statistics_target".
        cursor.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET STATISTICS -1")
        cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [table])
        cursor.execute(f"ALTER SEQUENCE {cursor.fetchone()[0]} RESTART WITH 1")

    assert model.objects.count() == 0
    assert _target(model, column) == _default_target()
    assert _sequence_value(model) == 1


# The one piece of state this cannot put back is the pg_statistic rows
# themselves: ANALYZE on an empty table leaves the previous run's entries alone
# rather than clearing them, and deleting from the catalogue directly needs
# rights a test suite should not have. It does not need to. Every build here
# ends in an ANALYZE that overwrites the table's statistics wholesale, so what a
# most-common-value assertion reads afterwards is this build's work -- and the
# target, which is what decides how much of it there is, is put back above.


def test_a_declared_target_reaches_the_column() -> None:
    _fresh(Bucketed, "code")

    build_shape(
        Shape(Table(Bucketed, rows=5_000, code=_wide_skew(), statistics={"code": 300}), seed=1)
    )

    assert _target(Bucketed, "code") == 300


def test_and_the_planner_records_more_of_the_shape_because_of_it() -> None:
    # The assertion that makes the one above worth having. A target the database
    # accepted and then ignored would satisfy pg_attribute and change nothing
    # the planner sees, so what is measured here is the most-common-value list
    # itself.
    #
    # Falsifiable two ways, and both matter: with the ALTER removed the list
    # stops at the default hundred, and with it moved to after the ANALYZE it
    # stops at a hundred as well -- a target changed after statistics are
    # gathered does nothing until the next gathering, which is the same ordering
    # trap as analyze-then-load. The second of those only fails because _fresh
    # puts the column's target back first; without it this test inherited the
    # target the test above had left behind and passed either way.
    _fresh(Bucketed, "code")

    build_shape(
        Shape(Table(Bucketed, rows=5_000, code=_wide_skew(), statistics={"code": 300}), seed=2)
    )

    assert _most_common(Bucketed, "code") > _default_target()


def test_a_shape_the_planner_could_not_record_is_refused_by_name() -> None:
    _fresh(Narrowed, "code")

    with pytest.raises(InvalidShape) as raised:
        build_shape(Shape(Table(Narrowed, rows=5_000, code=_wide_skew()), seed=3))

    message = str(raised.value)
    assert "Narrowed.code" in message
    assert str(_WIDE) in message
    assert str(_default_target()) in message
    # The remedy is in the message, spelled the way it would be written.
    assert "statistics={'code': 150}" in message


def test_the_refusal_happens_before_a_single_row_is_loaded() -> None:
    # A build refused after a two-million-row COPY is a refusal nobody thanks
    # you for, and the ordering that makes this true is the same ordering the
    # ALTER needs -- both belong before the load rather than beside the ANALYZE.
    #
    # An empty table proves nothing on its own: the build runs in a transaction,
    # so rows written and then refused are rolled back and the table is empty
    # either way. The sequence is the one trace a load leaves that a rollback
    # cannot remove -- setval is not transactional -- so it is what this reads.
    # And it is put back to one first rather than compared against itself,
    # because the test above already builds this table: without that, a load
    # that did happen moved the sequence to exactly where the previous test had
    # left it, and the comparison held.
    _fresh(Narrowed, "code")

    with pytest.raises(InvalidShape):
        build_shape(Shape(Table(Narrowed, rows=5_000, code=_wide_skew()), seed=4))

    assert Narrowed.objects.count() == 0
    assert _sequence_value(Narrowed) == 1


def test_declaring_the_target_is_what_makes_the_same_shape_buildable() -> None:
    # The pair the design rests on: the identical declaration is refused above
    # and accepted here, and the only difference is that this one asked.
    _fresh(Narrowed, "code")

    build_shape(
        Shape(Table(Narrowed, rows=5_000, code=_wide_skew(), statistics={"code": _WIDE}), seed=5)
    )

    assert Narrowed.objects.count() == 5_000


def test_a_distribution_that_cannot_count_its_values_is_left_alone() -> None:
    # Only a Bounded distribution can be checked, and a fan-out is not one --
    # nor is a distribution drawing from a continuous range. They are treated as
    # unbounded rather than as suspicious, which is the same reading Bounded was
    # written for.
    build_shape(
        Shape(
            Table(Template, rows=10, name=Constant("t")),
            Table(
                TemplateSession,
                rows=40,
                template=FanOut(Zipf()),
                title=Constant("s"),
                minutes=Constant(1),
            ),
            seed=6,
        )
    )

    assert TemplateSession.objects.count() == 40


def test_a_projection_takes_a_target_like_any_other_table() -> None:
    _fresh(TargetedSession, "title")

    build_shape(
        Shape(
            Table(Template, rows=10, name=Constant("t")),
            Table(
                TemplateSession,
                rows=40,
                template=FanOut(Zipf()),
                title=Constant("s"),
                minutes=Constant(1),
            ),
            Table(Event, rows=30, template=FanOut(Zipf()), name=Constant("e")),
            Projection(
                TargetedSession,
                per=Event,
                copying=TemplateSession,
                statistics={"title": 250},
            ),
            seed=7,
        )
    )

    assert _target(TargetedSession, "title") == 250
    assert TargetedSession.objects.count() > 0


# A stub rather than a second database, for the reason every backend branch in
# this package is written against a vendor: a path reachable only by running the
# suite on the backend it skips is a path the coverage gate cannot see.
_SQLITE = SimpleNamespace(vendor="sqlite")


@pytest.mark.django_db
def test_nothing_is_asked_of_a_backend_that_has_no_such_thing() -> None:
    # No cursor is opened at all, which is what the stub proves: a statement
    # against a connection this simple would raise.
    apply_statistics_targets(
        _SQLITE, Table(Bucketed, rows=5, code=Constant("a"), statistics={"code": 200})
    )
