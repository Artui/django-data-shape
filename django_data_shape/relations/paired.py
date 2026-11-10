from __future__ import annotations

from collections.abc import Sequence

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.distributions.distribution import Distribution


class Paired:
    """A relation filled with the opposite end of an edge, never repeating a pair.

    The declaration a through table needs; two ``FanOut``s beside one are refused."""

    def __init__(
        self,
        relation: str,
        weights: Distribution,
        *,
        parents: Sequence[object] | None = None,
    ) -> None:
        if not isinstance(relation, str) or not relation:
            raise InvalidShape(
                f"Paired needs the name of the fan-out it is paired with, got {relation!r}. "
                "A table with more than one foreign key cannot be left to guess which one the "
                "pairing is over."
            )
        self._relation = relation
        self._weights = weights
        self._parents = None if parents is None else tuple(parents)

    @property
    def relation(self) -> str:
        return self._relation

    @property
    def weights(self) -> Distribution:
        return self._weights

    @property
    def parents(self) -> tuple[object, ...] | None:
        """The partner keys this edge may use, or ``None`` for all of them."""
        return self._parents

    def canonical(self) -> object:
        """The fan-out it pairs with, the weights, and any narrowing. See ``Canonical``."""
        return (self._relation, self._weights, self._parents)

    def __repr__(self) -> str:
        if self._parents is None:
            return f"Paired({self._relation!r}, {self._weights!r})"
        return f"Paired({self._relation!r}, {self._weights!r}, parents={list(self._parents)!r})"
