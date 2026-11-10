from __future__ import annotations

from django_data_shape.derivations.scope import Scope


class Copied:
    """``relation.field``, unchanged.

    A ticket's face value is its order's unit price: there is no arithmetic at all."""

    def __init__(self, source: str) -> None:
        self._source = source

    @property
    def scope(self) -> Scope:
        return Scope.PARENT

    @property
    def sources(self) -> tuple[str, ...]:
        return (self._source,)

    def value(self, row: int, draw: float, sources: tuple[object, ...]) -> object:
        return sources[0]

    def canonical(self) -> object:
        return ("Copied", self._source)

    def __repr__(self) -> str:
        return f"Copied({self._source!r})"


__all__ = ["Copied"]
