from __future__ import annotations

import math
import numbers
from decimal import Decimal
from typing import Any

from django.db.models import Model

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.relations.fan_out_plan import FanOutPlan
from django_data_shape.relations.paired import Paired
from django_data_shape.relations.paired_plan import PairedPlan
from django_data_shape.utils import draw, field_stream, named_keys

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
    """Read the partner table's real keys and choose distinct ones per group."""
    named = named_keys(paired.parents, partner, table, field)
    keys = _read_partner_keys(partner, connection, named)
    _require_every_named_partner(named, keys, partner, table, field)
    keys = _in_declared_order(named, keys, partner)
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
            f"more {partner.__name__} rows, flatten {paired.relation}'s sizes, cap them with "
            f"max_children={len(keys)} on its FanOut, or ask for fewer rows here."
        )
    weights = _weights(paired, keys, seed, table, field) if rows else [1.0] * len(keys)
    return PairedPlan(
        keys=keys,
        sizes=sizes,
        weights=weights,
        bands=_band_of(weights),
        stream=field_stream(seed, table, f"{field}:pair"),
    )


def _read_partner_keys(
    partner: type[Model], connection: Any, named: tuple[object, ...] | None = None
) -> list[int]:
    """The partner's real keys, queried rather than assumed."""
    pk_column = partner._meta.pk.column
    quote = connection.ops.quote_name
    statement = (
        f"SELECT {quote(pk_column)} FROM {quote(partner._meta.db_table)} "
        f"ORDER BY {quote(pk_column)}"
    )
    params: tuple[object, ...] = ()
    if named is not None:
        placeholders = ", ".join(["%s"] * len(named)) or "NULL"
        statement = (
            f"SELECT {quote(pk_column)} FROM {quote(partner._meta.db_table)} "
            f"WHERE {quote(pk_column)} IN ({placeholders}) ORDER BY {quote(pk_column)}"
        )
        params = tuple(partner._meta.pk.get_db_prep_value(key, connection) for key in named)
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        return [row[0] for row in cursor.fetchall()]


def _require_every_named_partner(
    named: tuple[object, ...] | None,
    keys: list[int],
    partner: type[Model],
    table: str,
    field: str,
) -> None:
    """Every key ``parents=`` named has to be a row, or the narrowing is a lie."""
    if named is None:
        return
    present = {partner._meta.pk.to_python(key) for key in keys}
    missing = [key for key in named if key not in present]
    if not missing:
        return
    raise InvalidShape(
        f"{table}.{field} names partner key(s) {missing!r} in parents=, and "
        f"{partner.__name__} has no row with them. The narrowing runs in the database, so "
        "a key that matches nothing is not an error there -- the edges that would have used "
        "it go to the other named partners, or this table comes out empty. Load the partner "
        "first, or name keys that exist."
    )


def _in_declared_order(
    named: tuple[object, ...] | None, keys: list[int], partner: type[Model]
) -> list[int]:
    """Put named partners back in the order the declaration named them."""
    if named is None:
        return keys
    to_python = partner._meta.pk.to_python
    present = {to_python(key): key for key in keys}
    return [present[key] for key in named if key in present]


def _weights(paired: Paired, keys: list[int], seed: int, table: str, field: str) -> list[float]:
    """A weight per partner, scattered across the key range rather than ordered."""
    stream = field_stream(seed, table, f"{field}:weight")
    numeric: list[float] = []
    for index in range(len(keys)):
        weight = paired.weights.value(index, draw(stream, index))
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
    """Which weight band each partner falls in, heaviest band first."""
    if not weights:
        return []
    heaviest = max(weights)
    step = math.log(_BAND_RATIO)
    return [int(math.log(heaviest / weight) / step) if weight > 0 else 0 for weight in weights]
