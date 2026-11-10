from __future__ import annotations

import bisect
from collections.abc import Mapping
from math import gcd

from django_data_shape.utils import draw


class FanOutPlan:
    """Which parent owns each child row, decided once and answered in O(1).

    A partition of the child key range: parent ``j`` owns rows ``[starts[j], starts[j + 1])``."""

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
        """One of the owning parent's own columns, for one child row."""
        return self._parent_values[field][self._slot(row)]

    def group_position(self, row: int) -> tuple[int, int]:
        """Where this child sits inside its parent's group, and how big the group is."""
        slot = (row * self._stride) % self._rows
        index = bisect.bisect_right(self._starts, slot) - 1
        start = self._starts[index]
        end = self._starts[index + 1] if index + 1 < len(self._starts) else self._rows
        return slot - start, end - start

    def group_of(self, row: int) -> int:
        """Which parent this row belongs to, as an index into the parent keys."""
        return self._slot(row)

    def _slot(self, row: int) -> int:
        """Which parent owns this child row, as an index into ``keys``."""
        slot = (row * self._stride) % self._rows
        return bisect.bisect_right(self._starts, slot) - 1

    def sizes(self) -> list[int]:
        """How many children each parent ended up with, in parent-key order."""
        bounds = [*self._starts, self._rows]
        return [bounds[i + 1] - bounds[i] for i in range(len(self._keys))]

    def parent_keys(self) -> tuple[object, ...]:
        """The keys this partition was spread across, in the order they were read."""
        return tuple(self._keys)


def _stride(rows: int) -> int:
    """A multiplier that walks every slot exactly once, scattering as it goes."""
    if rows < 3:
        return 1
    candidate = max(2, int(rows * 0.6180339887498949))
    while gcd(candidate, rows) != 1:
        candidate += 1
    return candidate
