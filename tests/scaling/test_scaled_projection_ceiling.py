"""A projection's ceiling has to move with the factor, or it fires at scale.

`scaled_shape` multiplies every table's row count and passed a `Projection`
through untouched, which is right for its *count* -- that grows because the
tables it reads did -- and wrong for a declared ceiling, which is a number in the
same units and did not move.

The consumer that asked for `max_rows` hit this on the first run: a ceiling
sized for the world as written refused every growth assertion, because those
build the same declaration at a larger factor.
"""

from __future__ import annotations

import pytest
from django.db import connection

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
    scaled_shape,
    scaled_world,
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


def test_the_ceiling_is_multiplied_by_the_factor() -> None:
    scaled = scaled_shape(_shape(max_rows=1_000), 10)

    projection = next(t for t in scaled.tables if isinstance(t, Projection))
    assert projection.max_rows == 10_000


def test_a_declaration_with_no_ceiling_is_still_passed_through_untouched() -> None:
    shape = _shape(max_rows=None)
    original = next(t for t in shape.tables if isinstance(t, Projection))

    scaled = scaled_shape(shape, 10)

    assert next(t for t in scaled.tables if isinstance(t, Projection)) is original


def test_the_original_declaration_is_not_mutated() -> None:
    """A shape is inert data, and scaling it must not edit the one it came from."""
    shape = _shape(max_rows=1_000)

    scaled_shape(shape, 10)

    assert next(t for t in shape.tables if isinstance(t, Projection)).max_rows == 1_000


def test_a_scaled_world_builds_under_a_ceiling_sized_for_factor_one() -> None:
    """The consumer's case, end to end.

    Every table scales by the same factor, so a parent has the same number of
    children at every factor and the projection grows linearly -- which is why
    multiplying the ceiling by the factor is the right arithmetic and not an
    approximation of one.
    """
    at_one = _measure(1)

    with scaled_world(_shape(max_rows=at_one * 2), 10, using=connection.alias):
        assert EventSession.objects.count() > at_one


def test_a_ceiling_that_is_still_too_low_at_the_factor_refuses_by_name() -> None:
    """Scaling the ceiling does not disable it."""
    with (
        pytest.raises(InvalidShape, match="max_rows="),
        scaled_world(_shape(max_rows=1), 10, using=connection.alias),
    ):
        pass  # pragma: no cover - the build raises before the block runs


def _measure(factor: int) -> int:
    with scaled_world(_shape(max_rows=None), factor, using=connection.alias):
        return EventSession.objects.count()
