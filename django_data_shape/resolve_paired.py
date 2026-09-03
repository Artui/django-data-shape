"""Turning a paired declaration into distinct partners inside every group."""

from __future__ import annotations

import math
import numbers
from decimal import Decimal
from typing import Any

from django.db.models import Model

from django_data_shape.fan_out_plan import FanOutPlan
from django_data_shape.invalid_shape import InvalidShape
from django_data_shape.paired import Paired
from django_data_shape.paired_plan import PairedPlan
from django_data_shape.utils import draw, field_stream

# A band holds partners within this much of each other's weight, which is what
# makes the band count derived from the declaration rather than chosen. The
# allocation below converges as bands get finer, so the rule has to be past the
# limit rather than on it: measured across three shapes, tightening this from
# 2.0 to 1.03 -- thirteen bands to three hundred -- does not move the result,
# and this value is converged in all of them at about forty bands.
_BAND_RATIO = 1.3


def resolve_paired(
    paired: Paired,
    partner: type[Model],
    plan: FanOutPlan,
    rows: int,
    seed: int,
    table: str,
    field: str,
    connection: Any,
) -> PairedPlan:
    """Read the partner table's real keys and choose distinct ones per group.

    ``plan`` is the fan-out this pairs with, already resolved, because the
    groups it produced are what the partners are chosen *within*. That ordering
    is the whole mechanism: a group of size ``k`` takes ``k`` distinct partners,
    so no two rows of one group share a partner and no two rows of different
    groups share a pair.

    **The refusal that only becomes decidable here.** The busiest group needs as
    many distinct partners as it has rows, so the constraint is
    ``max group size <= partners`` -- not ``rows <= groups x partners``, which
    is the number a capacity check would compute and is far larger. A heavy tail
    puts a large share of every edge on one group, so a declaration that looks
    sparse can still be impossible: ``Zipf(1.2)`` over five thousand groups puts
    21% of the edges on the top one. It cannot be checked before the partition
    is resolved, so this is the one structural refusal that has to wait for the
    build.
    """
    keys = _read_partner_keys(partner, connection)
    sizes = plan.sizes()
    if not keys and rows:
        raise InvalidShape(
            f"{table}.{field} pairs over {partner.__name__}, which has no rows. Load it first, "
            "or declare it in the same shape so it is built before this table."
        )
    busiest = max(sizes) if sizes else 0
    if busiest > len(keys):
        raise InvalidShape(
            f"{table}.{field} pairs {rows} rows over {len(keys)} {partner.__name__} rows, and "
            f"the busiest {paired.relation} needs {busiest} distinct partners -- more than there "
            "are. Every row of one group needs a different partner, so what has to fit is the "
            "largest group against the partner count, not the row count against the product of "
            "the two. A heavy tail puts a large share of every edge on one group, so declare "
            f"more {partner.__name__} rows, flatten {paired.relation}'s sizes, or ask for fewer "
            "rows here."
        )
    # Nothing to place means nothing to weigh, the same guard the fan-out's own
    # sizes carry. Reaching the zero-total refusal below with an empty table
    # would refuse a shape that is merely empty, which is a legitimate thing to
    # declare -- it is what an edge table nobody has written to looks like.
    weights = _weights(paired, keys, seed, table, field) if rows else [1.0] * len(keys)
    return PairedPlan(
        keys=keys,
        sizes=sizes,
        weights=weights,
        bands=_band_of(weights),
        stream=field_stream(seed, table, f"{field}:pair"),
    )


def _read_partner_keys(partner: type[Model], connection: Any) -> list[int]:
    """The partner's real keys, queried rather than assumed.

    The same correction a fan-out carries: a project may have built the partner
    table with the ORM, so its keys are whatever the sequence handed out and a
    child pointing at ``1..N`` would point at nothing.
    """
    pk_column = partner._meta.pk.column
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {quote(pk_column)} FROM {quote(partner._meta.db_table)} "
            f"ORDER BY {quote(pk_column)}"
        )
        return [row[0] for row in cursor.fetchall()]


def _weights(paired: Paired, keys: list[int], seed: int, table: str, field: str) -> list[float]:
    """A weight per partner, scattered across the key range rather than ordered.

    Ordered weights would put a correlation between a partner's key and its
    popularity into this table, and a correlated foreign key is planner-visible
    -- the same reason a fan-out scatters its sizes.
    """
    stream = field_stream(seed, table, f"{field}:weight")
    numeric: list[float] = []
    for index in range(len(keys)):
        weight = paired.weights.value(index, draw(stream, index))
        # The same tower a fan-out's sizes are checked against, and for the same
        # reason: a rounded distribution hands back a Decimal, which is a Number
        # and deliberately not a Real.
        if isinstance(weight, bool) or not isinstance(weight, (numbers.Real, Decimal)):
            raise InvalidShape(
                f"{table}.{field} needs numeric pairing weights, but {paired.weights!r} produced "
                f"{weight!r}. A weight is how often a partner is chosen, so the distribution "
                "weighing it has to produce numbers -- int, float, Decimal and Fraction all work, "
                "and they are normalised so their scale does not matter."
            )
        numeric.append(float(weight))
    total = sum(numeric)
    if total <= 0:
        raise InvalidShape(
            f"{table}.{field} weighs every {paired.relation} partner at zero, so there is no "
            "distribution to choose them by. At least one weight has to be positive."
        )
    return numeric


def _band_of(weights: list[float]) -> list[int]:
    """Which weight band each partner falls in, heaviest band first.

    No partners is not a band of zero, it is no bands -- ``max`` of nothing has
    no answer and an empty table has no question.
    """
    if not weights:
        return []
    heaviest = max(weights)
    step = math.log(_BAND_RATIO)
    return [int(math.log(heaviest / weight) / step) if weight > 0 else 0 for weight in weights]
