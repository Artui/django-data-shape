"""A shape is inert data, and stays that way."""

from __future__ import annotations

import pytest

from django_data_shape import (
    Constant,
    FanOut,
    InvalidShape,
    Projection,
    Shape,
    Table,
    Uniform,
    Zipf,
)
from tests.testapp.models import Company, Event, EventSession, Left, Right, TemplateSession


def _company(rows: int = 1) -> Table:
    return Table(Company, rows=rows, name=Constant("acme"))


def test_it_holds_its_tables_and_seed() -> None:
    shape = Shape(_company(), seed=99)

    assert shape.seed == 99
    assert repr(shape) == "Shape(Company, seed=99)"


def test_the_seed_defaults_to_something_reproducible() -> None:
    # Not random. Two runs of an undeclared seed must still agree, or the
    # package's reproducibility claim holds only for callers who remembered.
    assert Shape(_company()).seed == Shape(_company()).seed


def test_a_shape_with_no_tables_is_refused() -> None:
    with pytest.raises(InvalidShape, match="at least one table"):
        Shape()


def test_the_same_table_declared_twice_is_refused() -> None:
    with pytest.raises(InvalidShape, match="declared twice"):
        Shape(_company(rows=1), _company(rows=2))


def test_it_has_no_build_method() -> None:
    # Building lives in a function so a shape stays hashable, serialisable data:
    # the template-database cache key and the shape-from emitter both need that
    # and neither can have it if a shape can hold a connection.
    assert not hasattr(Shape(_company()), "build")


def test_a_shape_cannot_be_edited_past_its_own_validation() -> None:
    shape = Shape(_company(), seed=1)

    for attribute, value in (("tables", ()), ("seed", 2)):
        with pytest.raises(AttributeError):
            setattr(shape, attribute, value)


def test_a_model_declared_as_both_a_table_and_a_projection_is_refused() -> None:
    # The same over-determination as declaring it twice, and worse for being
    # harder to see: one of the two would silently win by load order.
    with pytest.raises(InvalidShape, match="declared twice"):
        Shape(
            Table(
                EventSession, rows=5, event=FanOut(Zipf()), title=Constant("s"), minutes=Constant(1)
            ),
            Projection(EventSession, per=Event, copying=TemplateSession),
        )


def test_a_load_order_cycle_is_refused_where_the_shape_is_declared() -> None:
    # The last purely structural refusal this package deferred to build time.
    # Which declaration can be filled first is decided by the declarations and
    # nothing else, so a shape that could never be built was being constructed,
    # hashed and passed around before anything said so.
    with pytest.raises(InvalidShape, match="cycle"):
        Shape(
            Table(Left, rows=5, right=FanOut(Uniform(1, 2))),
            Table(Right, rows=5, left=FanOut(Uniform(1, 2))),
        )
