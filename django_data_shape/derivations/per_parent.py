"""One value for one row of every group, and another for the rest."""

from __future__ import annotations

from typing import cast

from django_data_shape.derivations.scope import Scope
from django_data_shape.distributions.categorical import Categorical
from django_data_shape.distributions.distribution import Distribution
from django_data_shape.invalid_shape import InvalidShape

# Not ``None``, because ``None`` is a value a caller legitimately wants on the
# special row: an SCD-2 validity chain ends with ``valid_to`` unset, and "the
# current period is the one with no end" is precisely a rule of this shape. A
# sentinel is the only way to tell "the last row holds nothing" apart from "no
# last row was declared".
_UNSET = object()


def _is_distribution(candidate: object) -> bool:
    """Whether this is a distribution rather than the value of a column.

    Structural on purpose, and structural on the **type** rather than on the
    instance. The one plain value that would otherwise be mistaken for a
    distribution is an ``Enum`` member -- ``Status.COMPLETE.value`` is the
    string it wraps, and an enum is an entirely ordinary thing to fill a status
    column with. ``type(member).value`` is the ``DynamicClassAttribute``
    descriptor rather than a method, so reading the attribute off the class
    tells the two apart where reading it off the instance would quietly refuse
    every enum.

    An ``isinstance`` against
    :class:`~django_data_shape.distributions.distribution.Distribution` would be
    the obvious spelling and is not available: it is a plain ``Protocol``, and
    making it runtime-checkable would check for the same attribute on the
    instance -- which is the check that gets enums wrong.
    """
    return callable(getattr(type(candidate), "value", None))


class PerParent:
    """Within each parent's children, one end of the group is different.

    The primitive for per-group business rules, and the reason it is one
    primitive rather than a family: *one active project per company*, *one
    default address per customer*, *one current period per subscription*, *one
    primary contact per account* and *N winners per contest* are the same
    declaration with different words.

    ```python
    Table(
        Project,
        rows=2_000_000,
        company=FanOut(Zipf(1.2), placement="grouped"),
        created_at=Sequential(start, step),
        status=PerParent("company", order_by="created_at", last="ACTIVE", rest="COMPLETE"),
    )
    ```

    **The count of special rows is derived, not declared, and that is the
    whole point.** Fifty thousand companies and two million projects means
    ``status='ACTIVE'`` matches one row per company that has any -- around 2.5%
    of the table -- and that number falls out of the fan-out rather than being
    chosen. Declaring ``Skew({"ACTIVE": 0.1, ...})`` beside the same fan-out
    asks for two hundred thousand active projects in a schema that permits fifty
    thousand, and it is the same rule this package states everywhere else: a
    distribution is declared over a fixed count, never as a multiplier. Here one
    distribution is *derived from* another rather than declared beside it.

    **Assignment order is not emission order**, and this is where that split
    earns its keep. To say "the last project", a group has to be known; to keep
    physical placement honest, rows have to be emitted interleaved. They
    reconcile because a ``FanOut`` is a **partition** of the child range rather
    than a draw per child, so a row's position within its parent's group is
    arithmetic on the row index and the seed. Nothing is buffered, nothing is
    sorted, and the rows still stream into ``COPY`` one at a time.

    ``last=`` puts the value on the final ``count`` positions of every group and
    ``first=`` on the opening ones; exactly one of the two is given. A group
    smaller than ``count`` has every row special, which is arithmetically right
    rather than a special case: a company with one project can have at most one
    active project. A **childless** parent contributes nothing at all, which is
    why the achieved count is one per *non-empty* group.

    ``rest=`` is the value every other row of the group takes. It may be a plain
    value, or a distribution that implements
    :class:`~django_data_shape.distributions.categorical.Categorical` -- and
    only that kind, checked here, because a distribution that could also produce
    the special value would put the count back under a coin flip and undo the
    derivation above. A ``Skew`` over the remaining statuses is accepted; a
    ``Uniform`` is refused, because nothing can ask it what it might emit.

    ``order_by=`` names the column whose ordering the group's positions agree
    with. It is a **claim that is checked, not a sort that is performed**: see
    :meth:`~django_data_shape.table.Table` for the two conditions that make it
    true, and note that it buys realism the planner cannot see -- Postgres keeps
    no statistic about which row of a group holds which value, so a shape
    without it has the same selectivity and the same plan.
    """

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
        # cast, not a guard: a GROUP source is resolved by this package out of
        # the fan-out's own partition, so the pair is a pair by construction and
        # a runtime check here would be a branch no declaration can reach.
        position, size = cast("tuple[int, int]", sources[0])
        # A group smaller than the count has every row special, and that falls
        # out of the arithmetic rather than being clamped: ``size - count`` goes
        # negative and every position is at or past it. Two entries cannot
        # produce three winners, and one project is the only one a company can
        # have active.
        at_the_special_end = (
            position >= size - self._count if self._from_last else position < self._count
        )
        if at_the_special_end:
            return self._special
        if self._rest_is_distribution:
            return cast("Distribution", self._rest).value(row, draw)
        return self._rest

    def canonical(self) -> object:
        """The relation, both ends, the count and the ordering claim. See ``Canonical``.

        ``order_by`` is in it although it changes no value in any row. What it
        changes is which declarations are accepted, and a shape that would be
        refused is not the same shape as one that would not -- so two of them
        must not share a cached database.
        """
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
