"""Distributions that can only ever produce so many different values."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Bounded(Protocol):
    """A distribution with a known, finite number of distinct values.

    Deliberately a **second** protocol rather than a method on
    :class:`~django_data_shape.distributions.distribution.Distribution`. Adding
    it there would make it required, and a custom distribution written against
    the single-method protocol would stop satisfying it -- so the one thing this
    exists to prevent, a declaration that cannot describe a database, would be
    bought by breaking every declaration someone had already written.

    Structural and runtime-checkable, so a distribution opts in by having the
    method and nothing has to register anywhere. A distribution that cannot
    answer -- one drawing from a continuous range, say -- simply does not
    implement it, and is treated as unbounded rather than as suspicious.
    """

    def distinct_values(self) -> int: ...
