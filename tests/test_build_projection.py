"""Filling a table from tables already built, and what that buys."""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.models import Model

from django_data_shape import (
    Constant,
    FanOut,
    InvalidShape,
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
from tests.testapp.models import (
    Attendance,
    AuditedSession,
    Event,
    EventSession,
    Template,
    TemplateSession,
)

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="COPY loading and planner statistics need PostgreSQL",
    ),
]


def _world(templates: int = 20, sessions: int = 120, events: int = 300) -> Shape:
    """The Template/Event graph, with the projection first in declaration order.

    First on purpose: the order the caller writes must not decide the order the
    tables are filled, and a projection is the declaration for which getting
    that wrong is silent -- it would insert nothing and leave a declared table
    empty rather than fail.
    """
    return Shape(
        Projection(EventSession, per=Event, copying=TemplateSession),
        Table(Template, rows=templates, name=Constant("t")),
        Table(
            TemplateSession,
            rows=sessions,
            template=FanOut(Zipf(1.1)),
            title=Skew({"morning": 0.5, "evening": 0.5}),
            # Distinct per row, so a copied column can be checked against the
            # row it was copied from rather than against a shared value every
            # source would satisfy.
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
    """What the join says the projected table has to hold.

    Counted off the built parents rather than restated from the declaration,
    because the claim under test is that the cardinality is *determined* by
    them: a number copied out of the declaration would agree with the bug.
    """
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
    # And it is reported back, which is what a declaration with no rows= of its
    # own has instead of a number the caller could assert beforehand.
    projected = next(table for table in result.tables if table.table == EventSession._meta.db_table)
    assert projected.rows == EventSession.objects.count()


def test_every_projected_row_mirrors_the_template_session_it_came_from() -> None:
    build_shape(_world())

    for session in EventSession.objects.select_related("event", "source")[:50]:
        assert session.source is not None
        # The collection copied is the one belonging to the event's template,
        # which is the sentence the whole feature exists to make true.
        assert session.source.template_id == session.event.template_id
        assert session.title == session.source.title
        assert session.minutes == session.source.minutes
        # A model default is written, because a Django default is not DDL and
        # the column would otherwise fail its not-null check; a nullable column
        # is left out of the statement altogether.
        assert session.channel == "web"
        assert session.note is None


def test_an_events_session_count_is_its_templates_session_count() -> None:
    build_shape(_world())

    sizes = _sessions_per_template()
    for event in Event.objects.all()[:40]:
        assert EventSession.objects.filter(event=event).count() == sizes[event.template_id]


def test_the_child_count_is_correlated_with_the_template_as_a_fan_out_cannot_be() -> None:
    # The reason this is a projection rather than a fan-out. A Zipf fan-out on
    # TemplateSession.template gives some templates many sessions and some few,
    # so every event built from a big template must have many sessions and every
    # event from a small one few. A FanOut declared on EventSession.event would
    # draw that count independently and destroy exactly this correlation,
    # handing the planner a join selectivity real data never has.
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
    # Not "larger on average": every single one, because the count is copied
    # rather than drawn. That is the difference the declaration makes.
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
    # Same models, same total row count, the child declared as a fan-out instead
    # of a projection: the count is then drawn per event rather than copied from
    # the template, so events built from one template disagree about how many
    # sessions they have. Under the projection every template's events agree
    # exactly, which is the cross-table correlation being declared.
    build_shape(_world())
    total = EventSession.objects.count()
    projected = _counts_per_template()
    assert all(len(counts) == 1 for counts in projected.values())
    # And not because every event got the same collection. "Constant within a
    # template" is satisfied by a cross join as easily as by the right one, so
    # the counts also have to differ across templates -- which is the half that
    # makes it a correlation with the template rather than a constant.
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
    # The precondition is set rather than inherited. A sequence is not
    # transactional and Django's flush between tests does not restart it, so a
    # sequence another test left high makes this pass whether or not the build
    # moved it -- which is how this test survived a mutation that skipped the
    # reset for exactly this kind of table.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT setval(pg_get_serial_sequence(%s, 'id'), 1, false)",
            [EventSession._meta.db_table],
        )

    build_shape(_world())
    event = Event.objects.first()
    source = TemplateSession.objects.first()

    # Without the reset this raises IntegrityError on a primary key that is
    # already taken, exactly as it would for a COPY-loaded table.
    made = EventSession.objects.create(event=event, source=source, title="x", minutes=1)

    assert made.pk == EventSession.objects.exclude(pk=made.pk).count() + 1


def _statistics_for(model: type[Model]) -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_stats WHERE tablename = %s", [model._meta.db_table])
        return int(cursor.fetchone()[0])


def test_the_projected_table_is_analyzed_like_any_other() -> None:
    # AuditedSession rather than EventSession, and the precondition asserted
    # rather than assumed. pg_statistic rows are not transactional and survive
    # the truncation between tests, so an assertion over a table some other test
    # analyzed passes whether or not this code ran -- which is exactly how this
    # test passed a mutation that removed the ANALYZE it is named after.
    assert _statistics_for(AuditedSession) == 0

    build_shape(
        Shape(
            *_world().tables,
            Projection(AuditedSession, per=Event, copying=TemplateSession),
            seed=3,
        )
    )

    # Rows arriving by INSERT ... SELECT are as invisible to the planner as rows
    # arriving by COPY. A projection that skipped this would ship the exact
    # unanalyzed table this package exists to condemn.
    assert _statistics_for(AuditedSession) > 0


def test_the_rows_land_in_the_order_they_would_have_arrived_in() -> None:
    build_shape(_world())

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT event_id FROM {EventSession._meta.db_table} ORDER BY ctid")
        physical = [row[0] for row in cursor.fetchall()]

    # Grouped by event and ascending in event key, which for a copied collection
    # is also its arrival order: one event's rows are written in one transaction,
    # and the events themselves arrive in key order. That is why there is no
    # placement= here -- the two orders a fan-out has to choose between are the
    # same order for a copy.
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
    # The ordering question a projection raises, answered rather than refused: a
    # table filled by a statement is still a table, and the sort puts it before
    # anything that reads it.
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
    # Half a graph is not a smaller world; it is a declared table left out of
    # the database, and every test reading it then passes for the wrong reason.
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
                # Every column the table needs, channel included: a model
                # default is applied by save() and is not DDL, so the escape
                # hatch has to write one the same way the derived form does.
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
    # The emptiness check, the sequence reset and the ANALYZE belong to the
    # build rather than to the statement, so an escape hatch still gets them.
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
