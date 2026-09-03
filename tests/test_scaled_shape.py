"""A factor varies the declaration, and this is what that has to mean."""

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
    # The reason every table scales rather than only the one under test: a
    # factor that grew the children alone would change the average fan-out along
    # with the size, so two worlds would differ in a second way and the curve
    # would no longer be about size.
    shape = Shape(
        Table(Company, rows=20, name=Constant("acme")),
        Table(Session, rows=200, label=Constant("x"), company=FanOut(Zipf())),
    )

    parents, children = (table.rows for table in scaled_shape(shape, 5).tables)

    assert (parents, children) == (100, 1000)
    assert children / parents == 200 / 20


def test_the_seed_survives_scaling() -> None:
    # Factor 1 and factor 10 have to be the same world at two sizes. A seed left
    # behind would make them two unrelated worlds, and a query count that
    # differed between them would say nothing about growth.
    assert scaled_shape(Shape(_orders(), seed=1234), 4).seed == 1234


def test_the_declared_distributions_survive_scaling() -> None:
    fan_out = FanOut(Zipf(), childless=0.3, placement="grouped")
    shape = Shape(
        Table(Company, rows=5, name=Constant("acme")),
        Table(Session, rows=50, label=Constant("x"), company=fan_out),
    )

    scaled = scaled_shape(shape, 3)

    # The same object, not merely an equal one: a scaled shape that rebuilt its
    # distributions would be a second declaration that happens to agree today.
    assert scaled.tables[1].fields["company"] is fan_out


def test_the_key_strategy_survives_scaling() -> None:
    # Reconstruction goes through Table's constructor, which infers a strategy
    # when none is passed. Passing the resolved one back is what keeps a caller's
    # own strategy from being silently replaced by an inferred one.
    keys = KeyFunction(lambda row: f"page-{row:04d}")
    shape = Shape(Table(SlugPk, rows=3, keys=keys, name=Constant("x")))

    assert scaled_shape(shape, 2).tables[0].keys is keys


def test_the_original_shape_is_untouched() -> None:
    shape = Shape(_orders(rows=100))

    scaled_shape(shape, 10)

    # A shape is inert data that a template cache will key on later. Scaling one
    # in place would change what an already-built world was built from.
    assert shape.tables[0].rows == 100


@pytest.mark.parametrize("factor", [0, -1])
def test_a_factor_below_one_is_refused(factor: int) -> None:
    with pytest.raises(InvalidShape, match="whole number of at least 1"):
        scaled_shape(Shape(_orders()), factor)


@pytest.mark.parametrize("factor", [1.5, 2.0])
def test_a_fractional_factor_is_refused(factor: float) -> None:
    # Refused rather than rounded. Rounding lets each table land on its own real
    # factor, so a parent and its children can drift apart for a reason nothing
    # in the declaration mentions.
    with pytest.raises(InvalidShape, match="whole number of at least 1"):
        scaled_shape(Shape(_orders()), factor)


def test_a_declaration_that_only_holds_at_its_own_size_is_refused_by_factor() -> None:
    # One row cannot collide with itself, so a Constant on a unique column is an
    # ordinary thing to write at rows=1 and refused at rows=2. The arithmetic is
    # one Table already makes; what the factor adds is that the row count in the
    # message is not one the caller ever wrote, so the message has to say where
    # it came from.
    #
    # This used to be three values over three rows, which was accepted because
    # the capacity cleared the row count. It is not any more: a draw is not a
    # permutation at any capacity, so the boundary that survives scaling is the
    # single row rather than the exact fit.
    shape = Shape(Table(Subscriber, rows=1, email=Constant("a@example.com")))

    with pytest.raises(InvalidShape, match="At scale factor 2") as raised:
        scaled_shape(shape, 2)

    assert "email" in str(raised.value)


def test_a_scaled_shape_is_a_shape_like_any_other() -> None:
    scaled = scaled_shape(Shape(_orders(rows=10), seed=7), 3)

    # Not a private variant: it reaches build() through the same door and reads
    # back the same way in a failure message.
    assert repr(scaled) == "Shape(Order, seed=7)"


def test_a_projection_passes_through_untouched_and_needs_no_factor() -> None:
    # It has no declared row count to multiply: its size is count(per JOIN
    # copying), so scaling the tables it reads scales it by the same amount
    # without anything being said. That is the determined-not-distributed
    # property paying for itself -- a mirroring vocabulary carrying a row count
    # would have needed a rule here, and would have had to choose between
    # scaling the count and scaling what the count was derived from.
    projection = Projection(EventSession, per=Event, copying=TemplateSession)
    shape = Shape(projection, Table(Event, rows=100, template=FanOut(Zipf()), name=Constant("e")))

    scaled = scaled_shape(shape, 10)

    assert scaled.tables[0] is projection
    assert scaled.tables[1].rows == 1000


def test_a_scaled_shape_still_checks_the_rules_the_shape_declared() -> None:
    # A growth assertion builds every world through this function, so an
    # invariant dropped here is a rule that stops being checked in exactly the
    # worlds nobody watches -- with no error and no warning.
    rule = Invariant("every company is a violation", sql="SELECT id FROM testapp_company")
    shape = Shape(Table(Company, rows=10, name=Constant("acme")), invariants=(rule,))

    assert scaled_shape(shape, 10).invariants == (rule,)


def test_a_scaled_table_keeps_the_statistics_target_it_declared() -> None:
    # Dropped, this makes a table that needs a raised target unscalable at any
    # factor at all, including 1 -- the world simply comes back described by
    # fewer buckets than the declaration asked for.
    shape = Shape(Table(Company, rows=10, name=Constant("acme"), statistics={"name": 300}))

    assert scaled_shape(shape, 10).tables[0].statistics == {"name": 300}


def test_scaling_at_factor_one_changes_nothing_about_the_declaration() -> None:
    # The identity that makes the two above hard to get wrong again: at factor 1
    # a scaled shape is the same declaration, so anything the copy loses shows
    # up here without needing a test per field. The digest is the comparison
    # that works, because it reduces a declaration to a value -- ``canonical``
    # returns the tables themselves, which compare by identity.
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
    """The guard against the next field being dropped as quietly as these two.

    ``scaled_shape`` reassembles both objects through their own constructors, so
    a parameter added to either is silently left behind unless somebody
    remembers this function. Nothing else notices: the rebuilt shape is valid,
    the suite passes, and the declaration just means less than it says. This
    fails the day a parameter is added, which is the day the choice is being
    made anyway.
    """
    parameters = set(inspect.signature(constructor.__init__).parameters) - {"self"}

    assert parameters == handled, (
        f"{constructor.__name__}.__init__ has parameters scaled_shape does not "
        f"account for: {sorted(parameters - handled)}. Forward it in "
        "scaled_shape, or add it here with a comment saying why it is not."
    )
