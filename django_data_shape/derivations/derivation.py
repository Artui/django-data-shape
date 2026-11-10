from __future__ import annotations

from typing import Protocol, runtime_checkable

from django_data_shape.derivations.scope import Scope


@runtime_checkable
class Derivation(Protocol):
    """Computes one column from values that are already known.

    Deliberately **not** a kind of ``Distribution``: its inputs are other columns, not a draw alone."""

    @property
    def scope(self) -> Scope: ...

    @property
    def sources(self) -> tuple[str, ...]: ...

    def value(self, row: int, draw: float, sources: tuple[object, ...]) -> object: ...
