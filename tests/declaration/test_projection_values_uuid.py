"""The documented ``values=`` idiom, executed against UUID-keyed models.

The example in ``SqlValue`` and in ``docs/projections.md`` used to be
``({per}.id * 31 + {source}.id * 17) % 5 + 1``, which is a type error on any
schema whose models carry ``id = UUIDField(primary_key=True)`` -- a shared
abstract base doing exactly that is an ordinary Django layout. So the first
thing a reader copied out of the documentation did not run, and it failed at
build time from inside a generated statement rather than at declaration.

The documentation's Python blocks are parsed by ``test_documentation.py`` and
nothing more, which is precisely why this shipped: the expression is a string
literal and parses perfectly. The guard for an expression has to run it.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.models import F, Q

from django_data_shape import (
    Constant,
    FanOut,
    Invariant,
    Projection,
    Shape,
    Skew,
    SqlValue,
    Table,
    Uniform,
    Zipf,
    build,
)
from tests.testapp.models import Depot, Region, Route, Shipment

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="hashtext and the ::bigint cast are PostgreSQL's; an expression is not portable",
    ),
]

# The replacement idiom, and every part of it is load-bearing:
#
# - `hashtext` gives a UUID per-row variation at all, where arithmetic has no
#   operator to offer;
# - `::bigint` before `abs` because `abs(-2147483648)` is `integer out of range`
#   -- hashtext returns int4 and that value is one of the ones it can return;
# - `abs` at all because PostgreSQL's `%` keeps the sign of the dividend, so
#   `hashtext(...) % 500` spans negatives and a measure column would hold them.
_VARIED = "abs(hashtext({per}.id::text || {source}.id::text)::bigint) % 500 + 100"


def _shape(**values: SqlValue) -> Shape:
    values.setdefault("settled_amount", SqlValue("0"))
    return Shape(
        Table(Region, rows=3, name=Constant("r")),
        Table(Depot, rows=12, region=FanOut(Uniform(1, 4)), name=Constant("d")),
        Table(
            Route,
            rows=20,
            region=FanOut(Zipf(1.1)),
            label=Skew({"north": 0.5, "south": 0.5}),
        ),
        Projection(Shipment, per=Depot, copying=Route, values=values),
        seed=5,
    )


def test_the_documented_expression_runs_on_uuid_keys() -> None:
    build(
        _shape(requested_amount=SqlValue(_VARIED), approved_amount=SqlValue("0")),
        require_statistics=False,
    )

    amounts = list(Shipment.objects.values_list("requested_amount", flat=True))
    assert amounts, "the projection wrote no rows, so nothing below is being checked"
    # Varied, which is the entire reason values= exists: one value across every
    # row is n_distinct = 1 and the exact shape a planner cannot use.
    assert len(set(amounts)) > 1
    # In the range the expression claims, which is what the two casts buy.
    assert all(100 <= amount < 600 for amount in amounts)


def test_integer_arithmetic_on_a_uuid_key_has_no_operator() -> None:
    # The falsification for the test above: it pins *why* the documented idiom
    # is written the way it is, so that a later simplification back to `id * 31`
    # fails here rather than in a consumer's build.
    with pytest.raises(Exception, match="uuid"):
        build(
            _shape(
                requested_amount=SqlValue("({per}.id * 31 + {source}.id * 17) % 5 + 1"),
                approved_amount=SqlValue("0"),
            ),
            require_statistics=False,
        )


def test_a_reference_carries_the_join_aliases_into_the_expression_it_names() -> None:
    # `{per}` and `{source}` inside a referenced expression are substituted
    # because resolution runs first and rendering runs once over the result.
    build(
        _shape(
            requested_amount=SqlValue(_VARIED),
            approved_amount=SqlValue("{values.requested_amount} - 100"),
        ),
        require_statistics=False,
    )

    rows = list(Shipment.objects.values_list("requested_amount", "approved_amount"))
    assert len({requested for requested, _approved in rows}) > 1, "expected a varied column"
    assert all(approved == requested - 100 for requested, approved in rows)


def test_the_stated_relationship_and_the_invariant_that_nets_it() -> None:
    # The pairing the documentation now recommends: the declaration states the
    # relationship, and an Invariant checks it once the rows are in. The second
    # is what catches a volatile expression, where substitution means the two
    # copies are two different values and the rule silently stops holding.
    shape = Shape(
        Table(Region, rows=3, name=Constant("r")),
        Table(Depot, rows=12, region=FanOut(Uniform(1, 4)), name=Constant("d")),
        Table(
            Route,
            rows=20,
            region=FanOut(Zipf(1.1)),
            label=Skew({"north": 0.5, "south": 0.5}),
        ),
        Projection(
            Shipment,
            per=Depot,
            copying=Route,
            values={
                "requested_amount": SqlValue(_VARIED),
                "approved_amount": SqlValue("{values.requested_amount} * 8 / 10"),
                "settled_amount": SqlValue("{values.approved_amount} / 2"),
            },
        ),
        invariants=[
            Invariant(
                "an approved amount never exceeds the amount requested",
                Shipment,
                violated_by=Q(approved_amount__gt=F("requested_amount")),
            )
        ],
        seed=5,
    )

    build(shape, require_statistics=False)

    assert not Shipment.objects.filter(approved_amount__gt=F("requested_amount")).exists()
    assert not Shipment.objects.filter(settled_amount__gt=F("approved_amount")).exists()
