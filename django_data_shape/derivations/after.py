"""A column that lands some way past the parent's."""

from __future__ import annotations

from typing import Any, cast

from django_data_shape.derivations.scope import Scope
from django_data_shape.invalid_shape import InvalidShape


class After:
    """``parent.field`` plus a gap of ``at_least`` up to ``at_least + within``.

    An order is created after its customer signed up; a payment settles after
    its invoice was issued. Left undeclared, the two dates are independent, and
    a date-range join over them has a selectivity no production database has --
    every combination occurs, including the half that cannot.

    ```python
    Table(
        Order,
        rows=2_000_000,
        account=FanOut(Zipf()),
        created_at=After("account.signed_up_at", within=timedelta(days=365)),
    )
    ```

    The gap is spread uniformly across ``within`` using this column's own draw,
    so it is a real spread rather than a fixed offset, and it is reproducible
    from the seed like everything else.

    Two things worth knowing before reaching for it. **The result is not
    monotonic with the row**, because the parents are not: a column filled this
    way has a low ``pg_stats.correlation`` where
    :class:`~django_data_shape.distributions.sequential.Sequential` gives a high
    one. That is honest -- real children of scattered parents arrive scattered --
    but it is a different physical shape, and an index scan is costed
    differently over it. And **the fan-out it reads through may not have a null
    share**, because a child with no parent has no value to be after; that is
    refused when the table is declared rather than discovered as a ``None`` in
    the arithmetic.

    ``within`` and ``at_least`` are in the column's own units: ``timedelta`` for
    a datetime column, a number for a numeric one. Anything supporting
    ``parent + offset`` and ``offset * float`` works, which is what makes this
    one class rather than a datetime one and a numeric one.
    """

    def __init__(self, source: str, *, within: Any, at_least: Any = None) -> None:
        # Zero has to come from the caller's own unit: ``timedelta(0)`` for a
        # date column and ``0`` for a numeric one, and this expression is the
        # only way to say it without asking which they meant.
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
        # cast rather than a narrowing check: what a parent column holds is
        # decided by the model, not by this package, and a runtime guard here
        # would be a branch no declaration can reach from the supported side.
        parent = cast("Any", sources[0])
        return parent + self._at_least + self._within * draw

    def canonical(self) -> object:
        """The source it reads and the gap it adds. See ``Canonical``."""
        return (self._sources, self._within, self._at_least)

    def __repr__(self) -> str:
        return f"After({self._sources[0]!r}, within={self._within!r}, at_least={self._at_least!r})"
