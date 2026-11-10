from __future__ import annotations

from typing import cast

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.derivations.scope import Scope
from django_data_shape.distributions.categorical import Categorical
from django_data_shape.distributions.distribution import Distribution

# Not ``None``, because ``None`` is a value a caller legitimately wants on the
# special row: an SCD-2 validity chain ends with ``valid_to`` unset, and "the
# current period is the one with no end" is precisely a rule of this shape. A
# sentinel is the only way to tell "the last row holds nothing" apart from "no
# last row was declared".
_UNSET = object()


def _is_distribution(candidate: object) -> bool:
    """Whether this is a distribution rather than the value of a column."""
    return callable(getattr(type(candidate), "value", None))


class PerParent:
    """Within each parent's children, one end of the group is different.

    The primitive for per-group business rules, such as *one active project per company*."""

    def __init__(
        self,
        relation: str,
        *,
        last: object = _UNSET,
        first: object = _UNSET,
        rest: object,
        count: int = 1,
        order_by: str | None = None,
    ) -> None:
        if (last is _UNSET) == (first is _UNSET):
            raise InvalidShape(
                f"PerParent({relation!r}) needs exactly one of last= and first=, which is what "
                "says which end of each group is the special one. It was given "
                + ("both" if last is not _UNSET else "neither")
                + "."
            )
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise InvalidShape(
                f"PerParent({relation!r}) needs a count of at least one, got {count!r}. A count "
                "of zero declares a rule that never applies, which is said by not declaring it."
            )
        special = first if last is _UNSET else last
        rest_is_distribution = _is_distribution(rest)
        if rest_is_distribution and not isinstance(rest, Categorical):
            raise InvalidShape(
                f"PerParent({relation!r}) draws the rest of each group from {rest!r}, which "
                "cannot say which values it produces. The rest has to be a plain value or a "
                "distribution that enumerates itself -- a Skew or a Constant -- because a "
                "distribution that might also emit "
                f"{special!r} would put the count of special rows back under a coin flip, and "
                "making that count exact is the whole of what this primitive is for."
            )
        if rest_is_distribution and any(
            candidate == special for candidate in cast("Categorical", rest).shares()
        ):
            raise InvalidShape(
                f"PerParent({relation!r}) puts {special!r} on {count} row(s) of each group and "
                f"draws the rest from {rest!r}, which lists {special!r} as well. Every group "
                "would then get an unpredictable number of them rather than exactly "
                f"{count}, which is the arithmetic a per-group constraint rests on."
            )
        self._relation = relation
        self._special = special
        self._from_last = last is not _UNSET
        self._rest = rest
        self._rest_is_distribution = rest_is_distribution
        self._count = count
        self._order_by = order_by

    @property
    def scope(self) -> Scope:
        return Scope.GROUP

    @property
    def sources(self) -> tuple[str, ...]:
        return (self._relation,)

    @property
    def relation(self) -> str:
        """The fan-out whose partition decides the groups."""
        return self._relation

    @property
    def special(self) -> object:
        """The value the end of each group takes."""
        return self._special

    @property
    def count(self) -> int:
        """How many rows of each group take it."""
        return self._count

    @property
    def rest(self) -> object:
        """The value, or the distribution, every other row of a group takes."""
        return self._rest

    @property
    def order_by(self) -> str | None:
        """The column whose ordering the group's positions claim to agree with."""
        return self._order_by

    def value(self, row: int, draw: float, sources: tuple[object, ...]) -> object:
        position, size = cast("tuple[int, int]", sources[0])
        # A group smaller than the count has every row special, since size - count goes negative.
        at_the_special_end = (
            position >= size - self._count if self._from_last else position < self._count
        )
        if at_the_special_end:
            return self._special
        if self._rest_is_distribution:
            return cast("Distribution", self._rest).value(row, draw)
        return self._rest

    def canonical(self) -> object:
        """The relation, both ends, the count and the ordering claim. See ``Canonical``."""
        return (
            self._relation,
            self._special,
            self._from_last,
            self._rest,
            self._count,
            self._order_by,
        )

    def __repr__(self) -> str:
        end = "last" if self._from_last else "first"
        return (
            f"PerParent({self._relation!r}, {end}={self._special!r}, rest={self._rest!r}, "
            f"count={self._count!r}, order_by={self._order_by!r})"
        )
