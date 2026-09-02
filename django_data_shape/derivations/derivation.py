"""The one contract every derivation is written against."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from django_data_shape.derivations.scope import Scope


@runtime_checkable
class Derivation(Protocol):
    """Computes one column from values that are already known.

    Deliberately **not** a kind of
    :class:`~django_data_shape.distributions.distribution.Distribution`, and the
    difference is not a technicality. A distribution answers *what is the
    marginal shape of this column across N rows* -- which is the question the
    query planner asks, and the only kind of answer that decides a plan. A
    derivation answers *given this row's other values, what is this one*, which
    is what a creation service encodes and what no planner can see. Keeping them
    separate types is what keeps the first kind enumerable later, when a shape
    has to be summarised for a statistics target or a cache key: a mechanism
    that let a derivation masquerade as a distribution would make that
    enumeration quietly wrong.

    Three members, and only the first is what varies between the faces:

    ``scope``
        Where ``sources`` are read from. See :class:`Scope`.

    ``sources``
        The names to resolve, in the order ``value`` will receive them.
        Resolution belongs entirely to the caller, so an implementation never
        touches a plan, a connection or another column.

    ``value``
        The value itself, from the row index, this column's own draw, and the
        resolved sources. ``draw`` is supplied even to implementations that
        ignore it, for the same reason
        :class:`~django_data_shape.distributions.distribution.Distribution`
        supplies both halves: it keeps the protocol single-shaped, so the
        resolver has one call to make and not three.

    Like a distribution, an implementation must be a pure function of its
    arguments. One that carried state between calls would make the same shape
    produce different data depending on the order rows were computed in, and
    computation order is precisely what this mechanism reserves the right to
    choose.
    """

    @property
    def scope(self) -> Scope: ...

    @property
    def sources(self) -> tuple[str, ...]: ...

    def value(self, row: int, draw: float, sources: tuple[object, ...]) -> object: ...
