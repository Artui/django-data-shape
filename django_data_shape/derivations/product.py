from __future__ import annotations

from typing import Any, cast

from django_data_shape.derivations.scope import Scope


class Product:
    """``left * right``, read from two other columns of this row.

    One of three shorthands for ``Derived``, which is the general case and takes a callable."""

    def __init__(self, left: str, right: str) -> None:
        self._left = left
        self._right = right

    @property
    def scope(self) -> Scope:
        return Scope.ROW

    @property
    def sources(self) -> tuple[str, ...]:
        return (self._left, self._right)

    def value(self, row: int, draw: float, sources: tuple[object, ...]) -> object:
        # A cast, not a guard, as the other derivations do: the model decides what a column holds.
        left, right = (cast("Any", source) for source in sources)
        return left * right

    def canonical(self) -> object:
        return ("Product", self._left, self._right)

    def __repr__(self) -> str:
        return f"Product({self._left!r}, {self._right!r})"


__all__ = ["Product"]
