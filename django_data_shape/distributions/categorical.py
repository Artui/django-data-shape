"""Distributions that can name their values and say how often each occurs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class Categorical(Protocol):
    """A distribution over a known set of values, with a known share for each.

    The fourth opt-in protocol, beside
    :class:`~django_data_shape.distributions.bounded.Bounded`,
    :class:`~django_data_shape.canonical.Canonical` and
    :class:`~django_data_shape.keys.sql_keys.SqlKeys`, and added for the one
    question a business invariant asks that none of the others can answer:
    **how many of these rows will carry this particular value?**

    That question is what turns a partial ``UniqueConstraint`` from an error
    message at row 700,000 into arithmetic at declaration time.
    ``one_active_project_per_company`` permits one row per company with
    ``status='ACTIVE'``; a ``Skew`` giving ``ACTIVE`` a tenth of two million
    rows asks for two hundred thousand of them. Both numbers are known before a
    row is generated, and only ``shares`` supplies the second.

    It is deliberately **not** the same protocol as ``Bounded``, although
    :class:`~django_data_shape.distributions.skew.Skew` and
    :class:`~django_data_shape.distributions.constant.Constant` implement both.
    ``Bounded`` answers *how many different values can this produce*, which is a
    question about capacity and is answerable by a distribution that could never
    enumerate itself -- a shuffled range of ten thousand integers, say.
    ``Categorical`` answers *which values, and in what proportion*, which is a
    question about content. A distribution that can answer the second can always
    answer the first; the reverse is not true, so joining them would have made
    the cheap claim cost the expensive one.

    ``shares`` returns each value mapped to its share of the rows, summing to
    one. The shares are the declaration's own arithmetic rather than a
    measurement: what comes back is what the declaration asked for, which is
    exactly what a refusal should quote back at it.

    A distribution that cannot enumerate itself simply does not implement this,
    and is treated as undecidable rather than as suspicious -- the constraint it
    might have broken is then left to the post-load check and to the database.
    """

    def shares(self) -> Mapping[object, float]: ...
