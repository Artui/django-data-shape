from __future__ import annotations

from collections.abc import Callable

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.derivations.scope import Scope


class Derived:
    """``compute`` over the named sources, in the named scope.

    The general case, and the one every other face is a shorthand for."""

    def __init__(
        self,
        *sources: str,
        compute: Callable[..., object],
        scope: Scope | str = Scope.ROW,
    ) -> None:
        if not sources:
            raise InvalidShape(
                "Derived needs at least one source; a column computed from nothing is a "
                "Constant, which says the same thing without a callable."
            )
        if not callable(compute):
            raise InvalidShape(f"Derived compute must be callable, got {compute!r}.")
        try:
            self._scope = Scope(scope)
        except ValueError:
            # Named here, because ValueError lists the enum's repr rather than the declared words.
            raise InvalidShape(
                f"Derived scope must be one of {', '.join(member.value for member in Scope)}, "
                f"got {scope!r}."
            ) from None
        self._sources = sources
        self._compute = compute

    @property
    def scope(self) -> Scope:
        return self._scope

    @property
    def sources(self) -> tuple[str, ...]:
        return self._sources

    def value(self, row: int, draw: float, sources: tuple[object, ...]) -> object:
        return self._compute(*sources)

    def __repr__(self) -> str:
        name = getattr(self._compute, "__name__", self._compute)
        sources = ", ".join(repr(source) for source in self._sources)
        return f"Derived({sources}, compute={name!r}, scope={self._scope.value!r})"
