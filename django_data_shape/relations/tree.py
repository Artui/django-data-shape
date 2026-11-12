from __future__ import annotations

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.distributions.distribution import Distribution


class Tree:
    """Each row's parent is an earlier row of the same table, or nobody.

    The self-referential relation -- an org unit's parent unit, a comment's
    reply-to, a category's parent category -- which a ``FanOut`` cannot fill,
    because a fan-out reads its parent's keys out of a table that is still
    empty while this one loads. A tree needs no read at all: this package owns
    the keys, so a row's parent key is the key it assigns to an earlier row
    index.

    **Acyclic by construction.** Every non-root row points at a row with a
    *lower index*, so following parents strictly descends and has to end at a
    root. Nothing is checked after the fact because nothing could go wrong.

    ``roots`` is the share of rows with no parent, which is why the column has
    to be nullable. The count is exact rather than drawn -- ``ceil(rows *
    roots)`` -- and spread evenly through the table, and the first row is always
    one of them, since there is no earlier row for it to point at. That is also
    why a share of zero is refused rather than read as "one root".

    ``sizes`` weighs how many children each row gets, as it does on a
    ``FanOut``: normalised, so only the spread matters, and **keyed on the
    row's position rather than ordered by it**. The largest families sit
    wherever their weights fell, not at the lowest keys. That is the part a
    naive construction gets wrong -- each child picking an earlier row by
    weight makes the first rows the whales purely because every later row could
    pick them -- and getting it wrong puts a correlation between a key and its
    family size into the table, which the planner can see.

    One correlation is left, and it is the rule's own price rather than the
    construction's: a row near the end has few rows after it to be its
    children, so the last rows are leaves whatever they weigh, and the children
    they could not take go to earlier rows by weight. See ``docs/relations.md``
    for how large that is.

    There is no ``placement``. Every parent is an earlier row, so the parent
    column climbs with the table whatever order a family is written in, which is
    also how a production tree looks. Nor is there a ``max_children``, a
    ``childless`` or a depth; the depth is whatever the sizes make it, and flat
    sizes, where every row has about one child, make long chains. Sizes of one
    value give no row two children, which is what fills a unique key to this table.
    """

    def __init__(self, *, roots: float, sizes: Distribution) -> None:
        if not isinstance(roots, (int, float)) or not 0 < roots < 1:
            raise InvalidShape(
                "Tree roots is the share of rows with no parent and must be in (0, 1), got "
                f"{roots!r}. The first row is always a root, since no earlier row exists for it "
                "to point at, so a tree cannot have none; and a share of 1 leaves every row a "
                "root, which is the column left out of the shape."
            )
        self._roots = float(roots)
        self._sizes = sizes

    @property
    def roots(self) -> float:
        return self._roots

    @property
    def sizes(self) -> Distribution:
        return self._sizes

    def canonical(self) -> object:
        """The root share and the size distribution. See ``Canonical``."""
        return (self._roots, self._sizes)

    def __repr__(self) -> str:
        return f"Tree(roots={self._roots!r}, sizes={self._sizes!r})"
