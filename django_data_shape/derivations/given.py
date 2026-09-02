"""A distribution chosen by the parent's value."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django_data_shape.derivations.scope import Scope
from django_data_shape.distributions.distribution import Distribution
from django_data_shape.invalid_shape import InvalidShape


class Given:
    """A different distribution per value of ``parent.field``.

    Conditional skew, and the reason it is worth declaring: a free account's
    tickets are mostly closed and an enterprise account's mostly open, so a
    query filtering on both the plan and the status matches far more or far
    fewer rows than the product of the two marginals suggests.

    ```python
    Table(
        Ticket,
        rows=2_000_000,
        account=FanOut(Zipf()),
        severity=Given(
            "account.plan",
            {
                "free": Skew({"low": 0.9, "high": 0.1}),
                "enterprise": Skew({"low": 0.4, "high": 0.6}),
            },
            default=Skew({"low": 0.7, "high": 0.3}),
        ),
    )
    ```

    Worth being honest about what this buys. Postgres's own
    ``CREATE STATISTICS`` cannot span tables, so the planner **still** estimates
    this pair as independent. Declaring it does not fix an estimate; it builds
    the database in which the wrong estimate is reproducible, which is the
    difference between knowing a query is mis-planned and being told so.

    ``default`` covers the parent values that were not listed. Without one, an
    unlisted value is refused **during the load**, naming the column and the
    value -- one of the very few refusals in this package that cannot happen at
    declaration time, because the parent's values live in the parent table and
    not in the declaration. Passing a default is how a declaration says it meant
    to cover the rest.
    """

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
        """The source, every case **in declaration order**, and the default.

        See ``Canonical``. The cases are ordered for the same reason a ``Skew``'s
        weights are: each one is a distribution whose own order decides values,
        and a mapping this package reordered would be a mapping it had changed.
        """
        return (self._sources, self._cases, self._default)

    def __repr__(self) -> str:
        return f"Given({self._sources[0]!r}, {self._cases!r}, default={self._default!r})"
