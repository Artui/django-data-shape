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
    SqlValue,
    Table,
    Uniform,
    Zipf,
    build,
    shape_digest,
)
from tests.testapp.models import Event, EventSession, Template, TemplateSession

pytestmark = pytest.mark.django_db


def _shape(expression: str = "({per}.id * 31 + {source}.id * 17) % 5 + 1") -> Shape:
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
            per=Event,
            copying=TemplateSession,
            values={"channel": SqlValue(expression)},
        ),
        seed=3,
    )


def test_a_projected_column_can_be_an_expression_over_the_join() -> None:
    build(_shape(), require_statistics=False)

    values = set(EventSession.objects.values_list("channel", flat=True))

    assert len(values) > 1, "the column has to be distributed, not a single default"
    assert {float(value) for value in values} <= {1, 2, 3, 4, 5}


def test_the_modulo_operator_reaches_the_database_as_an_operator() -> None:
    """A literal `%` survives, and it takes an escape the caller does not write."""
    build(_shape("({per}.id * 31 + {source}.id * 17) % 5 + 1"), require_statistics=False)

    values = {float(value) for value in EventSession.objects.values_list("channel", flat=True)}

    assert values == {1, 2, 3, 4, 5}


def test_the_derived_join_is_kept() -> None:
    """The whole point: `sql=` also works and costs the derived form."""
    build(_shape(), require_statistics=False)

    rows = EventSession.objects.count()
    assert rows > 0
    assert EventSession.objects.exclude(title="").count() == rows
    assert EventSession.objects.filter(event__isnull=True).count() == 0


def test_an_expression_naming_a_column_the_table_does_not_have_is_refused() -> None:
    with pytest.raises(InvalidShape, match="nonesuch"):
        Projection(
            EventSession,
            per=Event,
            copying=TemplateSession,
            values={"nonesuch": SqlValue("1")},
        )


def test_an_expression_for_a_column_the_source_already_carries_is_refused() -> None:
    """Two answers for one column is the over-determination refused everywhere."""
    with pytest.raises(InvalidShape, match="title"):
        Projection(
            EventSession,
            per=Event,
            copying=TemplateSession,
            values={"title": SqlValue("'x'")},
        )


def test_values_are_refused_beside_a_raw_statement() -> None:
    """`sql=` is the whole SELECT, so an expression has nowhere to go."""
    with pytest.raises(InvalidShape, match="sql="):
        Projection(
            EventSession,
            columns=("id", "event", "title", "minutes", "channel"),
            sql="SELECT 1, 1, 'x', 1, 'y'",
            values={"channel": SqlValue("1")},
        )


def test_the_expression_reaches_the_digest() -> None:
    """Two declarations differing only in the expression are two databases, so keys differ."""
    assert shape_digest(_shape("1")) != shape_digest(_shape("2"))


@pytest.mark.parametrize("bad", ["", "   ", None, 7])
def test_an_empty_expression_is_refused_at_declaration(bad: object) -> None:
    """Refusing at declaration avoids a database error raised far from the cause."""
    with pytest.raises(InvalidShape, match="SQL expression"):
        SqlValue(bad)  # type: ignore[arg-type]


def test_it_reports_itself() -> None:
    value = SqlValue("mod({per}.id, 5)")

    assert repr(value) == "SqlValue('mod({per}.id, 5)')"
    assert value.expression == "mod({per}.id, 5)"
