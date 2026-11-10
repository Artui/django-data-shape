from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

from django_data_shape import Constant, FanOut, InvalidShape, Projection, Shape, Skew, Table, Zipf
from django_data_shape import build as build_shape
from django_data_shape.loading.apply_statistics_targets import apply_statistics_targets
from tests.testapp.models import (
    Bucketed,
    CasedTable,
    DottedTable,
    Event,
    Narrowed,
    TargetedSession,
    Template,
    TemplateSession,
    UuidSession,
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
    # Descending weights make a skew, so the most-common-value list has something to read.
    return Skew({f"code-{index:03d}": float(values - index) for index in range(values)})


def _target(model: type, column: str) -> int:
    """The target PostgreSQL will actually use for one column, default included."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COALESCE(NULLIF(attstattarget, -1), "
            "current_setting('default_statistics_target')::int) "
            "FROM pg_attribute WHERE attrelid = %s::regclass AND attname = %s",
            [connection.ops.quote_name(model._meta.db_table), column],
        )
        return int(cursor.fetchone()[0])


def _default_target() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('default_statistics_target')::int")
        return int(cursor.fetchone()[0])


def _sequence_value(model: type) -> int:
    """Where this table's identity sequence has been left."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_serial_sequence(%s, 'id')",
            [connection.ops.quote_name(model._meta.db_table)],
        )
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
    """Put one table back to the state a test is entitled to assume it is in."""
    table = connection.ops.quote_name(model._meta.db_table)
    with connection.cursor() as cursor:
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
    # The assertion that makes the one above worth having.
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
    # A refusal after a two-million-row COPY is one nobody thanks you for.
    _fresh(Narrowed, "code")

    with pytest.raises(InvalidShape):
        build_shape(Shape(Table(Narrowed, rows=5_000, code=_wide_skew()), seed=4))

    assert Narrowed.objects.count() == 0
    assert _sequence_value(Narrowed) == 1


def test_declaring_the_target_is_what_makes_the_same_shape_buildable() -> None:
    # The identical declaration is refused without the target and accepted with it.
    _fresh(Narrowed, "code")

    build_shape(
        Shape(Table(Narrowed, rows=5_000, code=_wide_skew(), statistics={"code": _WIDE}), seed=5)
    )

    assert Narrowed.objects.count() == 5_000


def test_a_distribution_that_cannot_count_its_values_is_left_alone() -> None:
    # A fan-out or a continuous range is treated as unbounded, not as suspicious.
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


@pytest.mark.parametrize(
    "model", [CasedTable, DottedTable], ids=["a capital in the name", "a dot in the name"]
)
def test_a_table_whose_name_needs_quoting_gets_its_target(model: type) -> None:
    # regclass reads the name as an identifier, so it must be quoted as Django wrote it.
    _fresh(model, "code")

    build_shape(Shape(Table(model, rows=500, code=_wide_skew(), statistics={"code": 300}), seed=8))

    assert _target(model, "code") == 300


def test_nor_does_it_read_the_targets_of_the_table_its_name_folds_to() -> None:
    # The quiet half of the same mistake.
    _fresh(CasedTable, "code")
    with connection.cursor() as cursor:
        cursor.execute("CREATE TEMPORARY TABLE testapp_casedtable (code varchar(20))")
        cursor.execute("ALTER TABLE testapp_casedtable ALTER COLUMN code SET STATISTICS 1000")
    try:
        with pytest.raises(InvalidShape, match=r"CasedTable\.code declares 150 distinct values"):
            build_shape(Shape(Table(CasedTable, rows=500, code=_wide_skew()), seed=9))
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS pg_temp.testapp_casedtable")

    assert CasedTable.objects.count() == 0


def test_a_db_table_written_already_quoted_is_looked_up_as_django_created_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Django passes a pre-quoted db_table through, so the lookup quotes it the same way.
    _fresh(CasedTable, "code")
    monkeypatch.setattr(CasedTable._meta, "db_table", '"testapp_CasedTable"')

    apply_statistics_targets(
        connection, Table(CasedTable, rows=5, code=Constant("a"), statistics={"code": 200})
    )

    assert _target(CasedTable, "code") == 200


def _sessions_of(event: Event, statistics: dict[str, int] | None) -> Shape:
    return Shape(
        Table(
            UuidSession,
            rows=3,
            event=FanOut(Zipf(), parents=[event.pk]),
            title=Constant("built"),
            statistics=statistics,
        ),
        seed=5,
    )


def test_a_target_is_set_over_rows_the_caller_wrote_in_the_same_transaction() -> None:
    # Django's deferred foreign-key check is still queued on the table when the build runs.
    with transaction.atomic():
        template = Template.objects.create(name="caller")
        event = Event.objects.create(template=template, name="caller")
        UuidSession.objects.create(event=event, title="caller")

        with CaptureQueriesContext(connection) as captured:
            build_shape(_sessions_of(event, {"title": 200}))

        assert _target(UuidSession, "title") == 200
        assert UuidSession.objects.count() == 4
        transaction.set_rollback(True)

    statements = [query["sql"] for query in captured]
    fired = statements.index("SET CONSTRAINTS ALL IMMEDIATE")
    assert fired < min(
        position for position, sql in enumerate(statements) if sql.startswith("ALTER TABLE")
    )


def test_no_check_is_fired_for_a_table_that_sets_no_target() -> None:
    # Firing them for every table would move every statement count a capture around a build sees.
    with transaction.atomic():
        template = Template.objects.create(name="caller")
        event = Event.objects.create(template=template, name="caller")
        UuidSession.objects.create(event=event, title="caller")

        with CaptureQueriesContext(connection) as captured:
            build_shape(_sessions_of(event, None))

        transaction.set_rollback(True)

    assert not [query["sql"] for query in captured if query["sql"].startswith("SET CONSTRAINTS")]


def test_a_row_that_breaks_a_constraint_is_refused_before_the_target() -> None:
    # Firing the queued checks changes when a bad row fails, not whether.
    with transaction.atomic():
        template = Template.objects.create(name="caller")
        event = Event.objects.create(template=template, name="caller")
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO testapp_uuidsession (id, event_id, title) "
                "VALUES (gen_random_uuid(), %s, 'orphan')",
                [event.pk + 1000],
            )
            constraints = connection.introspection.get_constraints(cursor, "testapp_uuidsession")
        (foreign_key,) = (name for name, info in constraints.items() if info["foreign_key"])

        with pytest.raises(IntegrityError, match=foreign_key):
            build_shape(_sessions_of(event, {"title": 200}))

        transaction.set_rollback(True)

    assert _target(UuidSession, "title") == _default_target()


def test_firing_them_defers_every_constraint_for_the_transaction() -> None:
    # A temporary table rather than a model, because Django creates no such constraint by default.
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMPORARY TABLE shape_parent (id integer PRIMARY KEY)")
            cursor.execute(
                "CREATE TEMPORARY TABLE shape_child (parent_id integer "
                "REFERENCES shape_parent DEFERRABLE INITIALLY IMMEDIATE)"
            )
        template = Template.objects.create(name="caller")
        event = Event.objects.create(template=template, name="caller")

        build_shape(_sessions_of(event, {"title": 200}))

        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO shape_child VALUES (1)")
            with pytest.raises(IntegrityError):
                connection.check_constraints()
        transaction.set_rollback(True)


# A stub rather than a second database, for the reason every backend branch in
# this package is written against a vendor: a path reachable only by running the
# suite on the backend it skips is a path the coverage gate cannot see.
_SQLITE = SimpleNamespace(vendor="sqlite")


@pytest.mark.django_db
def test_nothing_is_asked_of_a_backend_that_has_no_such_thing() -> None:
    # No cursor is opened, so a statement against this stub would raise.
    apply_statistics_targets(
        _SQLITE, Table(Bucketed, rows=5, code=Constant("a"), statistics={"code": 200})
    )
