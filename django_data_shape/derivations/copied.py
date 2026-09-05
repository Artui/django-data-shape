"""A column taken verbatim from the parent's."""

from __future__ import annotations

from django_data_shape.derivations.scope import Scope


class Copied:
    """``relation.field``, unchanged.

    A ticket's face value is the unit price of the order it belongs to; a line's
    currency is its invoice's. There is no arithmetic at all, which is what made
    needing a lambda for it -- and losing the template-database cache with it --
    annoying rather than merely unfortunate. See
    :class:`~django_data_shape.derivations.product.Product`.

    Worth saying what this is *not*, because the two are easy to confuse. A
    denormalised copy is a column with its own statistics: the planner sees a
    distribution over the child table rather than a join, which is the whole
    reason schemas carry such columns and the reason a shaped database has to
    reproduce them. Reading the parent's column through the join is a different
    query with a different plan.
    """

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
