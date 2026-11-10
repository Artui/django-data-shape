from __future__ import annotations

import datetime
import inspect

import pytest

from django_data_shape import (
    Constant,
    FanOut,
    InvalidShape,
    Invariant,
    KeyFunction,
    Projection,
    Shape,
    Skew,
    Table,
    Zipf,
    scaled_shape,
    shape_digest,
)
from tests.testapp.models import (
    Company,
    Event,
    EventSession,
    Order,
    Session,
    SlugPk,
    Subscriber,
    TemplateSession,
)

# No database at all. Scaling is arithmetic over a declaration, so it is one of
# the parts that means the same thing on every backend -- which is also why the
# growth assertions it exists for are not gated on Postgres the way a plan
# assertion is.

_AWARE = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)


def _orders(rows: int = 100) -> Table:
    return Table(
        Order,
        rows=rows,
        status=Skew({"complete": 0.9, "pending": 0.1}),
        total=Constant("1.00"),
        created_at=Constant(_AWARE),
    )


def test_every_row_count_is_multiplied() -> None:
    shape = Shape(Table(Company, rows=7, name=Constant("acme")), _orders(rows=100))

    scaled = scaled_shape(shape, 10)

    assert [table.rows for table in scaled.tables] == [70, 1000]


def test_the_parent_scales_with_the_child() -> None:
    # Growing only the children would change average fan-out as well as size.
    shape = Shape(
        Table(Company, rows=20, name=Constant("acme")),
        Table(Session, rows=200, label=Constant("x"), company=FanOut(Zipf())),
    )

    parents, children = (table.rows for table in scaled_shape(shape, 5).tables)

    assert (parents, children) == (100, 1000)
    assert children / parents == 200 / 20


def test_the_seed_survives_scaling() -> None:
    # Factor 1 and factor 10 have to be the same world at two sizes.
    assert scaled_shape(Shape(_orders(), seed=1234), 4).seed == 1234


def test_the_declared_distributions_survive_scaling() -> None:
    fan_out = FanOut(Zipf(), childless=0.3, placement="grouped", max_children=40)
    shape = Shape(
        Table(Company, rows=5, name=Constant("acme")),
        Table(Session, rows=50, label=Constant("x"), company=fan_out),
    )

    scaled = scaled_shape(shape, 3)

    # The cap is a business ceiling, so it does not scale with the factor.
    assert scaled.tables[1].fields["company"] is fan_out
    assert fan_out.max_children == 40


def test_the_key_strategy_survives_scaling() -> None:
    # Reconstruction goes through Table's constructor, which infers a strategy when none is passed.
    keys = KeyFunction(lambda row: f"page-{row:04d}")
    shape = Shape(Table(SlugPk, rows=3, keys=keys, name=Constant("x")))

    assert scaled_shape(shape, 2).tables[0].keys is keys


def test_the_original_shape_is_untouched() -> None:
    shape = Shape(_orders(rows=100))

    scaled_shape(shape, 10)

    # A shape is inert data that a template cache will key on later.
    assert shape.tables[0].rows == 100


@pytest.mark.parametrize("factor", [0, -1])
def test_a_factor_below_one_is_refused(factor: int) -> None:
    with pytest.raises(InvalidShape, match="whole number of at least 1"):
        scaled_shape(Shape(_orders()), factor)


@pytest.mark.parametrize("factor", [1.5, 2.0])
def test_a_fractional_factor_is_refused(factor: float) -> None:
    # Refused rather than rounded.
    with pytest.raises(InvalidShape, match="whole number of at least 1"):
        scaled_shape(Shape(_orders()), factor)


def test_a_declaration_that_only_holds_at_its_own_size_is_refused_by_factor() -> None:
    # A Constant on a unique column is fine at one row and refused at two.
    shape = Shape(Table(Subscriber, rows=1, email=Constant("a@example.com")))

    with pytest.raises(InvalidShape, match="At scale factor 2") as raised:
        scaled_shape(shape, 2)

    assert "email" in str(raised.value)


def test_a_scaled_shape_is_a_shape_like_any_other() -> None:
    scaled = scaled_shape(Shape(_orders(rows=10), seed=7), 3)

    # It reaches build() through the same door and reads back the same way in a failure.
    assert repr(scaled) == "Shape(Order, seed=7)"


def test_a_projection_passes_through_untouched_and_needs_no_factor() -> None:
    # Its size is a join's count, so scaling the tables it reads scales it too.
    projection = Projection(EventSession, per=Event, copying=TemplateSession)
    shape = Shape(projection, Table(Event, rows=100, template=FanOut(Zipf()), name=Constant("e")))

    scaled = scaled_shape(shape, 10)

    assert scaled.tables[0] is projection
    assert scaled.tables[1].rows == 1000


def test_a_scaled_shape_still_checks_the_rules_the_shape_declared() -> None:
    # An invariant dropped here stops being checked in exactly the worlds nobody watches.
    rule = Invariant("every company is a violation", sql="SELECT id FROM testapp_company")
    shape = Shape(Table(Company, rows=10, name=Constant("acme")), invariants=(rule,))

    assert scaled_shape(shape, 10).invariants == (rule,)


def test_a_scaled_table_keeps_the_statistics_target_it_declared() -> None:
    # Dropped, a raised statistics target would vanish even at factor 1.
    shape = Shape(Table(Company, rows=10, name=Constant("acme"), statistics={"name": 300}))

    assert scaled_shape(shape, 10).tables[0].statistics == {"name": 300}


def test_scaling_at_factor_one_changes_nothing_about_the_declaration() -> None:
    # At factor 1 a scaled shape is the same declaration, so any copy loss shows here.
    rule = Invariant("every company is a violation", sql="SELECT id FROM testapp_company")
    table = Table(Company, rows=10, name=Constant("acme"), statistics={"name": 300})
    shape = Shape(table, seed=7, invariants=(rule,))

    assert shape_digest(scaled_shape(shape, 1)) == shape_digest(shape)


@pytest.mark.parametrize(
    ("constructor", "handled"),
    [
        (Table, {"model", "rows", "fields", "keys", "statistics", "field_distributions"}),
        (Shape, {"tables", "seed", "invariants"}),
    ],
)
def test_every_constructor_parameter_is_accounted_for_when_a_shape_is_rebuilt(
    constructor: type, handled: set[str]
) -> None:
    """The guard against the next field being dropped as quietly as these two."""
    parameters = set(inspect.signature(constructor.__init__).parameters) - {"self"}

    assert parameters == handled, (
        f"{constructor.__name__}.__init__ has parameters scaled_shape does not "
        f"account for: {sorted(parameters - handled)}. Forward it in "
        "scaled_shape, or add it here with a comment saying why it is not."
    )
