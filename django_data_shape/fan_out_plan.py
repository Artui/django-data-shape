"""A fan-out resolved against real parent keys."""

from __future__ import annotations

import bisect
from collections.abc import Mapping
from math import gcd

from django_data_shape.utils import draw


class FanOutPlan:
    """Which parent owns each child row, decided once and answered in O(1).

    A **partition of the child key range**: parent ``j`` owns rows
    ``[starts[j], starts[j + 1])``. That representation is the whole point, and
    it was chosen over the obvious alternative -- drawing a parent per child --
    because a per-child draw **cannot be inverted**. Asking "which children
    belong to parent T" is what a mirrored collection needs, and against a draw
    the only answer is to index every row.

    Two consequences fall out for free. A childless parent is one whose range is
    empty, so the tail everybody forgets is representable rather than
    approximated. And physical placement becomes a pure question of the order
    rows are *emitted* in, entirely separate from which parent owns them -- the
    same split this design keeps finding between one order and another.
    """

    def __init__(
        self,
        keys: list[int],
        starts: list[int],
        rows: int,
        null_stream: int,
        null_share: float,
        interleave: bool,
        parent_values: Mapping[str, list[object]] | None = None,
    ) -> None:
        self._keys = keys
        self._starts = starts
        self._rows = rows
        self._null_stream = null_stream
        self._null_share = null_share
        self._stride = _stride(rows) if interleave else 1
        self._parent_values = parent_values or {}

    def key_for(self, row: int) -> int | None:
        """The parent key for one child row, or None where the key is null."""
        if self._null_share and draw(self._null_stream, row) < self._null_share:
            return None
        return self._keys[self._slot(row)]

    def parent_value(self, field: str, row: int) -> object:
        """One of the owning parent's own columns, for one child row.

        The third thing the partition representation buys, after the childless
        tail and free inversion: a child reaches across the edge in O(1) with no
        query of its own, because which parent owns it is already arithmetic.

        No null case, and that is enforced at declaration time rather than
        handled here -- a derivation reading across a fan-out with a null share
        is refused by ``Table``, because a child with no parent has no value to
        read and quietly substituting one would be the approximation this
        package refuses everywhere else.
        """
        return self._parent_values[field][self._slot(row)]

    def _slot(self, row: int) -> int:
        """Which parent owns this child row, as an index into ``keys``."""
        slot = (row * self._stride) % self._rows
        # bisect_right, not left: a parent with an empty range shares its start
        # with the next one, and bisect_right steps past every duplicate to the
        # last parent whose range actually begins at or below the slot. That is
        # what makes a childless parent unreachable rather than special-cased.
        return bisect.bisect_right(self._starts, slot) - 1

    def sizes(self) -> list[int]:
        """How many children each parent ended up with, in parent-key order."""
        bounds = [*self._starts, self._rows]
        return [bounds[i + 1] - bounds[i] for i in range(len(self._keys))]


def _stride(rows: int) -> int:
    """A multiplier that walks every slot exactly once, scattering as it goes.

    ``row * stride % rows`` is a bijection precisely when the two are coprime,
    so children land in an order unrelated to their parent without buffering a
    permutation or holding any state. Starting near the golden ratio of ``rows``
    gives the low-discrepancy spread that makes consecutive children come from
    unrelated parents, which is what "arrival order" means physically.
    """
    if rows < 3:
        return 1
    candidate = max(2, int(rows * 0.6180339887498949))
    while gcd(candidate, rows) != 1:
        candidate += 1
    return candidate
