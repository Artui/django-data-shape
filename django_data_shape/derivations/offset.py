from __future__ import annotations

from typing import Any, cast

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.derivations.scope import Scope


class Offset:
    """``source + by``, where ``source`` is another column of this row.

    The same-row half of ``After``, which is parent-scoped only."""

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
        # Cast, as After does: the column's type is the model's business, not a guard's.
        source = cast("Any", sources[0])
        return source + self._by

    def canonical(self) -> object:
        return ("Offset", self._source, self._by)

    def __repr__(self) -> str:
        return f"Offset({self._source!r}, by={self._by!r})"


__all__ = ["Offset"]
