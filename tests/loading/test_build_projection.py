from __future__ import annotations

import pytest
from django.db import connection
from django.db.models import Model

from django_data_shape import (
    Constant,
    FanOut,
    InvalidShape,
    Md5Keys,
    Projection,
    Sequential,
    Shape,
    ShapeNotEmpty,
    Skew,
    Table,
    Uniform,
    Zipf,
)
from django_data_shape import build as build_shape
from django_data_shape.utils import field_stream
from tests.testapp.models import (
    Attendance,
    AuditedSession,
    Event,
    EventSession,
    KeyedSession,
    Plan,
    PlanRun,
    PlanStage,
    Portfolio,
    RunStage,
    Template,
    TemplateSession,
    UuidSession,
)

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="COPY loading and planner statistics need PostgreSQL",
    ),
]


def _world(templates: int = 20, sessions: int = 120, events: int = 300) -> Shape:
    """The Template/Event graph, with the projection first in declaration order."""
    return Shape(
        Projection(EventSession, per=Event, copying=TemplateSession),
        Table(Template, rows=templates, name=Constant("t")),
        Table(
            TemplateSession,
            rows=sessions,
            template=FanOut(Zipf(1.1)),
            title=Skew({"morning": 0.5, "evening": 0.5}),
            minutes=Sequential(15, 1),
        ),
        Table(Event, rows=events, template=FanOut(Uniform(1, 4)), name=Constant("e")),
        seed=3,
    )


def _sessions_per_template() -> dict[int, int]:
    return {
        template_id: TemplateSession.objects.filter(template_id=template_id).count()
        for template_id in Template.objects.values_list("id", flat=True)
    }


def _expected_rows() -> int:
    """What the join says the projected table has to hold."""
    sizes = _sessions_per_template()
    return sum(
        sizes[template_id] for template_id in Event.objects.values_list("template_id", flat=True)
    )


def _reset_world() -> None:
    EventSession.objects.all().delete()
    Event.objects.all().delete()
    TemplateSession.objects.all().delete()
    Template.objects.all().delete()


def test_the_cardinality_is_determined_by_the_join_rather_than_declared() -> None:
    result = build_shape(_world())

    assert EventSession.objects.count() == _expected_rows()
    # It is reported back, since no rows= is declared to assert against beforehand.
    projected = next(table for table in result.tables if table.table == EventSession._meta.db_table)
    assert projected.rows == EventSession.objects.count()


def test_every_projected_row_mirrors_the_template_session_it_came_from() -> None:
    build_shape(_world())

    for session in EventSession.objects.select_related("event", "source")[:50]:
        assert session.source is not None
        assert session.source.template_id == session.event.template_id
        assert session.title == session.source.title
        assert session.minutes == session.source.minutes
        # The collection copied is the event's template's, which is what the feature exists for.
        assert session.channel == "web"
        assert session.note is None


def test_an_events_session_count_is_its_templates_session_count() -> None:
    build_shape(_world())

    sizes = _sessions_per_template()
    for event in Event.objects.all()[:40]:
        assert EventSession.objects.filter(event=event).count() == sizes[event.template_id]


def test_the_child_count_is_correlated_with_the_template_as_a_fan_out_cannot_be() -> None:
    # The reason this is a projection rather than a fan-out.
    build_shape(_world())

    sizes = _sessions_per_template()
    biggest = max(sizes, key=lambda template_id: sizes[template_id])
    smallest = min(sizes, key=lambda template_id: sizes[template_id])
    assert sizes[biggest] > sizes[smallest]

    from_biggest = [
        EventSession.objects.filter(event=event).count()
        for event in Event.objects.filter(template_id=biggest)
    ]
    from_smallest = [
        EventSession.objects.filter(event=event).count()
        for event in Event.objects.filter(template_id=smallest)
    ]
    assert from_biggest and from_smallest
    assert min(from_biggest) > max(from_smallest)


def _counts_per_template() -> dict[int, set[int]]:
    """How many sessions each of a template's events ended up with."""
    grouped: dict[int, set[int]] = {}
    for event in Event.objects.all():
        grouped.setdefault(event.template_id, set()).add(
            EventSession.objects.filter(event=event).count()
        )
    return grouped


def test_a_fan_out_over_the_same_graph_does_not_produce_this_shape() -> None:
    # The contrast that makes the claim above checkable rather than argued.
    build_shape(_world())
    total = EventSession.objects.count()
    projected = _counts_per_template()
    assert all(len(counts) == 1 for counts in projected.values())
    assert len({next(iter(counts)) for counts in projected.values()}) > 1

    EventSession.objects.all().delete()
    build_shape(
        Shape(
            Table(
                EventSession,
                rows=total,
                event=FanOut(Zipf(1.1)),
                title=Constant("s"),
                minutes=Constant(30),
            ),
            seed=3,
        )
    )

    drawn = _counts_per_template()
    assert any(len(counts) > 1 for counts in drawn.values())


def test_the_keys_are_the_dense_range_the_strategy_says_they_are() -> None:
    build_shape(_world())

    keys = sorted(EventSession.objects.values_list("id", flat=True))

    assert keys == list(range(1, EventSession.objects.count() + 1))


def test_the_sequence_is_moved_past_the_keys_the_statement_assigned() -> None:
    # The precondition is set rather than inherited.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT setval(pg_get_serial_sequence(%s, 'id'), 1, false)",
            [EventSession._meta.db_table],
        )

    build_shape(_world())
    event = Event.objects.first()
    source = TemplateSession.objects.first()

    made = EventSession.objects.create(event=event, source=source, title="x", minutes=1)

    assert made.pk == EventSession.objects.exclude(pk=made.pk).count() + 1


def _statistics_for(model: type[Model]) -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_stats WHERE tablename = %s", [model._meta.db_table])
        return int(cursor.fetchone()[0])


def test_the_projected_table_is_analyzed_like_any_other() -> None:
    # AuditedSession rather than EventSession, and the precondition asserted rather than assumed.
    assert _statistics_for(AuditedSession) == 0

    build_shape(
        Shape(
            *_world().tables,
            Projection(AuditedSession, per=Event, copying=TemplateSession),
            seed=3,
        )
    )

    assert _statistics_for(AuditedSession) > 0


def test_the_rows_land_in_the_order_they_would_have_arrived_in() -> None:
    build_shape(_world())

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT event_id FROM {EventSession._meta.db_table} ORDER BY ctid")
        physical = [row[0] for row in cursor.fetchall()]

    # Grouped by event and ascending, which for a copied collection is its arrival order.
    assert physical == sorted(physical)


def test_two_builds_of_one_shape_agree() -> None:
    build_shape(_world())
    first = list(EventSession.objects.order_by("id").values_list("id", "event_id", "source_id"))

    _reset_world()
    build_shape(_world())

    assert (
        list(EventSession.objects.order_by("id").values_list("id", "event_id", "source_id"))
        == first
    )


def test_a_projected_table_can_itself_be_a_fan_out_parent() -> None:
    # A table filled by a statement is still a table, so the sort puts it before its readers.
    shape = Shape(
        Table(Attendance, rows=400, session=FanOut(Zipf()), name=Constant("a")),
        *_world().tables,
        seed=3,
    )

    build_shape(shape)

    parents = set(EventSession.objects.values_list("id", flat=True))
    children = set(Attendance.objects.values_list("session_id", flat=True))
    assert Attendance.objects.count() == 400
    assert children <= parents


def test_a_projection_that_inserts_nothing_is_refused() -> None:
    # Half a graph is not a smaller world; every test reading it passes for the wrong reason.
    shape = Shape(
        Table(Template, rows=5, name=Constant("t")),
        Table(Event, rows=5, template=FanOut(Uniform(1, 2)), name=Constant("e")),
        Projection(EventSession, per=Event, copying=TemplateSession),
    )

    with pytest.raises(InvalidShape, match="inserted no rows") as raised:
        build_shape(shape)

    assert "Event, TemplateSession" in str(raised.value)


def test_a_projection_over_a_table_that_already_holds_rows_is_refused() -> None:
    build_shape(_world())

    with pytest.raises(ShapeNotEmpty, match=EventSession._meta.db_table):
        build_shape(Shape(Projection(EventSession, per=Event, copying=TemplateSession)))


def test_a_statement_of_your_own_gets_everything_but_the_derivation() -> None:
    build_shape(_world())
    expected = _expected_rows()
    EventSession.objects.all().delete()

    build_shape(
        Shape(
            Projection(
                EventSession,
                # A model default is not DDL, so the escape hatch must write the column itself.
                columns=("id", "event", "title", "minutes", "channel"),
                sql=(
                    "SELECT row_number() OVER (ORDER BY e.id, t.id), e.id, t.title, %s, %s "
                    f"FROM {Event._meta.db_table} e "
                    f"JOIN {TemplateSession._meta.db_table} t "
                    "ON t.template_id = e.template_id"
                ),
                params=(99, "import"),
            )
        )
    )

    assert EventSession.objects.count() == expected
    assert set(EventSession.objects.values_list("minutes", flat=True)) == {99}
    assert set(EventSession.objects.values_list("channel", flat=True)) == {"import"}
    assert sorted(EventSession.objects.values_list("id", flat=True)) == list(range(1, expected + 1))


def test_a_statement_of_your_own_that_inserts_nothing_is_refused_too() -> None:
    with pytest.raises(InvalidShape, match="inserted no rows") as raised:
        build_shape(
            Shape(
                Projection(
                    EventSession,
                    columns=("id", "event", "title", "minutes", "channel"),
                    sql=(
                        f"SELECT 1, e.id, 'x', 1, 'web' FROM {Event._meta.db_table} e WHERE false"
                    ),
                )
            )
        )

    assert "whatever the statement you supplied selects from" in str(raised.value)


def _events_into_sessions(reads: tuple[type[Model], ...] = ()) -> Projection:
    """A statement of the caller's own, selecting from one table it may name."""
    return Projection(
        EventSession,
        columns=("id", "event", "title", "minutes", "channel"),
        sql=(
            "SELECT row_number() OVER (ORDER BY e.id), e.id, e.name, 1, 'web' "
            f"FROM {Event._meta.db_table} e"
        ),
        reads=reads,
    )


def test_a_statement_of_your_own_is_ordered_after_the_tables_it_names() -> None:
    # Only reads= puts the statement between Event and the table it fills.
    build_shape(
        Shape(
            Table(Attendance, rows=40, session=FanOut(Zipf(1.1)), name=Constant("a")),
            _events_into_sessions(reads=(Event,)),
            Table(Template, rows=5, name=Constant("t")),
            Table(Event, rows=20, template=FanOut(Zipf(1.1)), name=Constant("e")),
        )
    )

    assert EventSession.objects.count() == 20
    assert Attendance.objects.count() == 40


def test_the_same_statement_without_reads_is_told_what_is_missing() -> None:
    # Without reads=, a statement could run before its inputs, and the build says so.
    with pytest.raises(InvalidShape, match="inserted no rows") as raised:
        build_shape(
            Shape(
                Table(Attendance, rows=40, session=FanOut(Zipf(1.1)), name=Constant("a")),
                _events_into_sessions(),
                Table(Template, rows=5, name=Constant("t")),
                Table(Event, rows=20, template=FanOut(Zipf(1.1)), name=Constant("e")),
            )
        )

    assert "name its inputs with reads=" in str(raised.value)


def test_a_uuid_keyed_projection_works_where_the_key_stream_exceeds_a_bigint() -> None:
    """The half of all tables that could not be projected at all."""
    from django_data_shape.utils import field_stream

    seed = 3
    assert field_stream(seed, KeyedSession._meta.db_table, ":key") > 2**63 - 1

    shape = Shape(
        Projection(KeyedSession, per=Event, copying=TemplateSession, keys=Md5Keys()),
        Table(Template, rows=5, name=Constant("t")),
        Table(
            TemplateSession,
            rows=20,
            template=FanOut(Constant(1)),
            title=Constant("s"),
            minutes=Sequential(15, 1),
        ),
        Table(Event, rows=12, template=FanOut(Uniform(1, 3)), name=Constant("e")),
        seed=seed,
    )

    build_shape(shape)

    keys = list(KeyedSession.objects.values_list("id", flat=True))
    assert keys
    stream = field_stream(seed, KeyedSession._meta.db_table, ":key")
    assert set(keys) == {Md5Keys().key_for(row, stream) for row in range(len(keys))}


def test_a_uuid_keyed_table_can_be_projected_once_its_keys_have_a_sql_form() -> None:
    """The case the refusal used to end, built end to end."""
    shape = Shape(
        Projection(UuidSession, per=Event, copying=TemplateSession, keys=Md5Keys()),
        Table(Template, rows=5, name=Constant("t")),
        Table(
            TemplateSession,
            rows=20,
            template=FanOut(Constant(1)),
            title=Constant("s"),
            minutes=Sequential(15, 1),
        ),
        Table(Event, rows=12, template=FanOut(Uniform(1, 3)), name=Constant("e")),
        seed=3,
    )

    build_shape(shape)

    keys = list(UuidSession.objects.order_by("id").values_list("id", flat=True))
    assert keys
    assert all(key.version == 4 for key in keys)
    stream = field_stream(shape.seed, UuidSession._meta.db_table, ":key")
    assert set(keys) == {Md5Keys().key_for(row, stream) for row in range(len(keys))}


def test_a_join_named_with_through_builds_the_collection_it_names() -> None:
    """The schema shape that had no derived form at all, built end to end."""
    shape = Shape(
        Projection(RunStage, per=PlanRun, copying=PlanStage, through=Plan),
        Table(Portfolio, rows=2, name=Constant("f")),
        Table(Plan, rows=4, name=Constant("p")),
        Table(
            PlanStage,
            rows=20,
            plan=FanOut(Uniform(1, 4)),
            portfolio=FanOut(Uniform(1, 2)),
            title=Sequential("s", "x"),
        ),
        Table(
            PlanRun,
            rows=10,
            plan=FanOut(Uniform(1, 3)),
            portfolio=FanOut(Uniform(1, 2)),
            name=Constant("r"),
        ),
        seed=3,
    )

    build_shape(shape)

    stages_per_plan = {
        plan_id: PlanStage.objects.filter(plan_id=plan_id).count()
        for plan_id in Plan.objects.values_list("id", flat=True)
    }
    runs = list(PlanRun.objects.all())
    assert runs
    for run in runs:
        copied = RunStage.objects.filter(run=run)
        assert copied.count() == stages_per_plan[run.plan_id]
        assert set(copied.values_list("title", flat=True)) == set(
            PlanStage.objects.filter(plan_id=run.plan_id).values_list("title", flat=True)
        )
