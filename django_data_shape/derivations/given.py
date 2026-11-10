from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.derivations.scope import Scope
from django_data_shape.distributions.distribution import Distribution


class Given:
    """A different distribution per value of ``parent.field``.

    Conditional skew: a free account's tickets are mostly closed, an enterprise account's open."""

    def __init__(
        self,
        source: str,
        cases: Mapping[Any, Distribution],
        default: Distribution | None = None,
    ) -> None:
        if not cases:
            raise InvalidShape(
                f"Given({source!r}) needs at least one case; with none it is whatever default "
                "was passed, which is a plain distribution."
            )
        self._sources = (source,)
        self._cases = dict(cases)
        self._default = default

    @property
    def scope(self) -> Scope:
        return Scope.PARENT

    @property
    def sources(self) -> tuple[str, ...]:
        return self._sources

    def value(self, row: int, draw: float, sources: tuple[object, ...]) -> object:
        chosen = self._cases.get(sources[0], self._default)
        if chosen is None:
            raise InvalidShape(
                f"Given({self._sources[0]!r}) has no case for {sources[0]!r} and no default, so "
                f"there is no distribution to draw this row from. Its cases are: "
                f"{', '.join(sorted(repr(case) for case in self._cases))}. Add the value, or pass "
                "default= to say the rest are covered."
            )
        return chosen.value(row, draw)

    def canonical(self) -> object:
        """The source, every case in declaration order, and the default. See ``Canonical``."""
        return (self._sources, self._cases, self._default)

    def __repr__(self) -> str:
        return f"Given({self._sources[0]!r}, {self._cases!r}, default={self._default!r})"
