from __future__ import annotations

from collections.abc import Sequence

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.distributions.distribution import Distribution

PLACEMENTS = ("arrival", "grouped")


class FanOut:
    """A distribution over how many children each parent has, over an already fixed child count.

    Never a multiplier: rows times fan-out would make the cardinality emergent."""

    def __init__(
        self,
        sizes: Distribution,
        *,
        parents: Sequence[object] | None = None,
        childless: float = 0.0,
        null: float = 0.0,
        placement: str = "arrival",
        max_children: int | None = None,
    ) -> None:
        for name, share in (("childless", childless), ("null", null)):
            if not 0.0 <= share < 1.0:
                raise InvalidShape(
                    f"FanOut {name} is a share of the whole and must be in [0, 1), got {share}."
                )
        if placement not in PLACEMENTS:
            raise InvalidShape(
                f"FanOut placement must be one of {', '.join(PLACEMENTS)}, got {placement!r}."
            )
        if max_children is not None and (
            isinstance(max_children, bool) or not isinstance(max_children, int) or max_children < 1
        ):
            raise InvalidShape(
                f"FanOut max_children is the most children any one parent may have, so it must "
                f"be a whole number of at least 1, got {max_children!r}."
            )
        narrowed = None if parents is None else tuple(parents)
        if narrowed is not None:
            # An empty sequence is falsy, so ``is None`` is what tells it from an undeclared cap.
            if not narrowed:
                raise InvalidShape(
                    "FanOut parents= names no parent, so there is nothing to spread these rows "
                    "over. Leave it out to use every parent in the table, or name at least one."
                )
            seen: set[object] = set()
            for key in narrowed:
                if key in seen:
                    raise InvalidShape(
                        f"FanOut parents= names {key!r} more than once. A repeated key weighs "
                        "that parent twice, which is a different partition from the one this "
                        "declaration reads as."
                    )
                seen.add(key)
        self._sizes = sizes
        self._parents = narrowed
        self._childless = childless
        self._null = null
        self._placement = placement
        self._max_children = max_children

    @property
    def sizes(self) -> Distribution:
        return self._sizes

    @property
    def parents(self) -> tuple[object, ...] | None:
        return self._parents

    @property
    def childless(self) -> float:
        return self._childless

    @property
    def null(self) -> float:
        return self._null

    @property
    def placement(self) -> str:
        return self._placement

    @property
    def max_children(self) -> int | None:
        return self._max_children

    def canonical(self) -> object:
        """The size distribution, the parents it spreads over, and the shares. See ``Canonical``."""
        return (
            self._sizes,
            self._parents,
            self._childless,
            self._null,
            self._placement,
            self._max_children,
        )

    def __repr__(self) -> str:
        narrowed = "" if self._parents is None else f"parents={self._parents!r}, "
        capped = "" if self._max_children is None else f", max_children={self._max_children!r}"
        return (
            f"FanOut({self._sizes!r}, {narrowed}childless={self._childless!r}, "
            f"null={self._null!r}, placement={self._placement!r}{capped})"
        )
