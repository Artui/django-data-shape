from __future__ import annotations

import pytest

from django_data_shape import (
    Constant,
    FanOut,
    InvalidShape,
    Projection,
    Sequential,
    Shape,
    Skew,
    Table,
    Uniform,
    Zipf,
    build,
)
from tests.testapp.models import Event, EventSession, Template, TemplateSession

pytestmark = pytest.mark.django_db


def _shape(*, max_rows: int | None) -> Shape:
    return Shape(
        Projection(EventSession, per=Event, copying=TemplateSession, max_rows=max_rows),
        Table(Template, rows=4, name=Constant("t")),
        Table(
            TemplateSession,
            rows=40,
            template=FanOut(Zipf(1.1)),
            title=Skew({"morning": 0.5, "evening": 0.5}),
            minutes=Sequential(15, 1),
        ),
        Table(Event, rows=20, template=FanOut(Uniform(1, 4)), name=Constant("e")),
        seed=3,
    )


def test_a_projection_under_its_ceiling_builds() -> None:
    result = build(_shape(max_rows=100_000), require_statistics=False)

    projected = next(table for table in result.tables if table.table == "testapp_eventsession")
    assert projected.rows > 0


def test_a_projection_over_its_ceiling_is_refused_before_it_inserts() -> None:
    with pytest.raises(InvalidShape) as excinfo:
        build(_shape(max_rows=3), require_statistics=False)

    message = str(excinfo.value)
    assert "testapp_eventsession" in message
    assert "max_rows=3" in message
    assert "would insert" in message

    # The refusal reports the count, so the reader can judge the ceiling or the declaration.
    assert EventSession.objects.count() == 0


def test_the_refusal_names_what_drives_the_size() -> None:
    """The number surprises because it is a product, so the message says so."""
    with pytest.raises(InvalidShape, match="Event"):
        build(_shape(max_rows=3), require_statistics=False)


def test_no_ceiling_costs_no_query() -> None:
    """A declaration that does not ask is not charged for the answer."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as without:
        build(_shape(max_rows=None), require_statistics=False)

    counted = [q for q in without.captured_queries if "count(" in q["sql"].lower()]
    assert counted == []


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_a_ceiling_below_one_is_refused_at_declaration(bad: int) -> None:
    with pytest.raises(InvalidShape, match="max_rows"):
        Projection(EventSession, per=Event, copying=TemplateSession, max_rows=bad)


def test_a_ceiling_that_is_not_a_whole_number_is_refused() -> None:
    """True is an int to isinstance, and a ceiling of one row is never what a caller meant."""
    with pytest.raises(InvalidShape, match="max_rows"):
        Projection(EventSession, per=Event, copying=TemplateSession, max_rows=True)


def _raw(*, max_rows: int) -> Shape:
    """The escape hatch, whose count has to wrap rather than count a join."""
    return Shape(
        Table(Template, rows=4, name=Constant("t")),
        Table(
            TemplateSession,
            rows=40,
            template=FanOut(Zipf(1.1)),
            title=Skew({"morning": 0.5, "evening": 0.5}),
            minutes=Sequential(15, 1),
        ),
        Table(Event, rows=20, template=FanOut(Uniform(1, 4)), name=Constant("e")),
        Projection(
            EventSession,
            columns=("id", "event", "title", "minutes", "channel"),
            sql=(
                "SELECT row_number() OVER (ORDER BY e.id, t.id), e.id, t.title, %s, %s "
                f"FROM {Event._meta.db_table} e "
                f"JOIN {TemplateSession._meta.db_table} t "
                "ON t.template_id = e.template_id"
            ),
            params=(99, "import"),
            reads=(Event, TemplateSession),
            max_rows=max_rows,
        ),
        seed=3,
    )


def test_a_raw_projection_is_counted_by_wrapping_the_callers_select() -> None:
    with pytest.raises(InvalidShape, match="max_rows=3"):
        build(_raw(max_rows=3), require_statistics=False)

    assert EventSession.objects.count() == 0


def test_a_raw_projection_under_its_ceiling_builds() -> None:
    build(_raw(max_rows=100_000), require_statistics=False)

    assert EventSession.objects.count() > 0
