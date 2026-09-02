"""How a foreign key's children are spread across their parents."""

from __future__ import annotations

from django_data_shape.distributions.distribution import Distribution
from django_data_shape.invalid_shape import InvalidShape

PLACEMENTS = ("arrival", "grouped")


class FanOut:
    """A distribution over how many children each parent has.

    Declared as a shape over an **already fixed** child row count, never as a
    multiplier. Rows times fan-out makes cardinality emergent, and a table whose
    size is emergent is a table nothing can assert about.

    ``sizes`` supplies a relative weight per parent -- ``Zipf()`` for the
    realistic heavy tail, ``Uniform(1, 10)`` for something flatter. The weights
    are normalised, so their scale is irrelevant and only their spread matters.

    ``childless`` is the share of parents with **no** children at all. It is
    called out separately because it is the case hand-written fixtures always
    omit and the one that changes what a join does: a parent nobody references
    is the difference between an inner and an outer join returning the same
    thing and returning different things.

    ``null`` is the share of children whose foreign key is NULL, which is only
    meaningful on a nullable column. **It thins the partition uniformly after**
    it is computed, so ``sizes`` describes the pre-null spread. Stated because
    the alternative -- partitioning only the non-null children -- would make the
    declared distribution mean something subtly different from what it says.

    ``placement`` decides where children sit physically, and it is not
    cosmetic. Emitting them parent by parent gives a perfectly clustered table
    that no production system has and that flatters every index scan; the
    default interleaves them the way rows really arrive.
    """

    def __init__(
        self,
        sizes: Distribution,
        *,
        childless: float = 0.0,
        null: float = 0.0,
        placement: str = "arrival",
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
        self._sizes = sizes
        self._childless = childless
        self._null = null
        self._placement = placement

    @property
    def sizes(self) -> Distribution:
        return self._sizes

    @property
    def childless(self) -> float:
        return self._childless

    @property
    def null(self) -> float:
        return self._null

    @property
    def placement(self) -> str:
        return self._placement

    def canonical(self) -> object:
        """The size distribution and the three shares that shape it. See ``Canonical``."""
        return (self._sizes, self._childless, self._null, self._placement)

    def __repr__(self) -> str:
        return (
            f"FanOut({self._sizes!r}, childless={self._childless!r}, "
            f"null={self._null!r}, placement={self._placement!r})"
        )
