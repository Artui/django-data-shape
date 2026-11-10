from __future__ import annotations

from typing import Any, cast

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.derivations.scope import Scope


class After:
    """``parent.field`` plus a gap of ``at_least`` up to ``at_least + within``.

    An order is created after its customer signed up, and a payment after its invoice was issued."""

    def __init__(self, source: str, *, within: Any, at_least: Any = None) -> None:
        # Zero must come in the caller's own unit: a timedelta for a date, 0 for a number.
        zero = within * 0
        if within < zero:
            raise InvalidShape(f"After needs a non-negative within, got {within!r}.")
        if at_least is None:
            at_least = zero
        elif at_least < at_least * 0:
            raise InvalidShape(
                f"After needs a non-negative at_least, got {at_least!r}. A negative gap puts the "
                "child before the parent, which is what this exists to prevent."
            )
        self._sources = (source,)
        self._within = within
        self._at_least = at_least

    @property
    def scope(self) -> Scope:
        return Scope.PARENT

    @property
    def sources(self) -> tuple[str, ...]:
        return self._sources

    def value(self, row: int, draw: float, sources: tuple[object, ...]) -> object:
        # A cast, not a guard: the model decides what a parent column holds, not this package.
        parent = cast("Any", sources[0])
        return parent + self._at_least + self._within * draw

    def canonical(self) -> object:
        """The source it reads and the gap it adds. See ``Canonical``."""
        return (self._sources, self._within, self._at_least)

    def __repr__(self) -> str:
        return f"After({self._sources[0]!r}, within={self._within!r}, at_least={self._at_least!r})"
