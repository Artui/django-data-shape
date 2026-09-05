"""The product of two columns of the same row."""

from __future__ import annotations

from typing import Any, cast

from django_data_shape.derivations.scope import Scope


class Product:
    """``left * right``, read from two other columns of this row.

    One of three derivations that exist because
    :class:`~django_data_shape.derivations.derived.Derived` -- the only shipped
    face that can read another column of the same row -- takes a callable, and
    a callable cannot be digested. A shape holding one is refused by
    :func:`~django_data_shape.template_database.template_database`, so a column
    as ordinary as ``total = quantity * unit_price`` excluded the whole
    declaration from the reuse that turns a forty-second build into a
    hundred-millisecond clone.

    That refusal is right and stays: two lambdas share a name, and identical
    bytecode returns something else when a constant it reads is edited in
    another module. What was wrong is what it excluded. These three say the
    commonest arithmetic as **data**, so they implement
    :class:`~django_data_shape.canonical.Canonical` and the shape hashes::

        Table(
            Order,
            rows=2_000_000,
            quantity=Aligned("basket", Uniform(1, 8, places=0)),
            unit_price=Aligned("basket", Uniform(1500, 25000, places=0)),
            total=Product("quantity", "unit_price"),
        )

    ``Derived`` is unchanged and remains the answer for computation that really
    is code. The line between them is whether the declaration can be written
    down: a product of two named columns can, a lambda cannot.
    """

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
        # cast rather than a narrowing check, as the other derivations do: what
        # a column holds is decided by the model, not by this package, and a
        # runtime guard would be a branch no declaration can reach.
        left, right = (cast("Any", source) for source in sources)
        return left * right

    def canonical(self) -> object:
        return ("Product", self._left, self._right)

    def __repr__(self) -> str:
        return f"Product({self._left!r}, {self._right!r})"


__all__ = ["Product"]
