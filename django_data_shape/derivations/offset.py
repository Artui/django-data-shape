"""A fixed distance past another column of the same row."""

from __future__ import annotations

from typing import Any, cast

from django_data_shape.derivations.scope import Scope
from django_data_shape.invalid_shape import InvalidShape


class Offset:
    """``source + by``, where ``source`` is another column of this row.

    The same-row half of :class:`~django_data_shape.derivations.after.After`,
    which is parent-scoped only. A show goes on sale and then happens; an
    invoice is issued and then falls due. Both columns are on the same row, and
    until this existed the only way to say so was a lambda -- which cost the
    whole shape its template-database cache. See
    :class:`~django_data_shape.derivations.product.Product` for why that
    mattered.

    The gap is **fixed**, which is the difference from ``After``: that one
    spreads a gap across ``within`` using the column's own draw, because a
    child's distance from its parent is a real distribution. A due date thirty
    days after an issue date is not a distribution, it is a term. Where the
    spread is wanted on the same row, ``Derived`` still takes a callable.

    ``by`` is in the column's own units -- a ``timedelta`` for a datetime
    column, a number for a numeric one -- and anything supporting
    ``source + by`` works.
    """

    def __init__(self, source: str, *, by: Any) -> None:
        zero = by * 0
        if by < zero:
            raise InvalidShape(
                f"Offset needs a non-negative by, got {by!r}. A column that lands "
                "*before* another is that other column's Offset, declared the other "
                "way round -- which keeps the two readable in the order they happen."
            )
        self._source = source
        self._by = by

    @property
    def scope(self) -> Scope:
        return Scope.ROW

    @property
    def sources(self) -> tuple[str, ...]:
        return (self._source,)

    def value(self, row: int, draw: float, sources: tuple[object, ...]) -> object:
        # cast for the same reason After does: the column's type is the model's
        # business, and a guard here is a branch no declaration reaches.
        source = cast("Any", sources[0])
        return source + self._by

    def canonical(self) -> object:
        return ("Offset", self._source, self._by)

    def __repr__(self) -> str:
        return f"Offset({self._source!r}, by={self._by!r})"


__all__ = ["Offset"]
