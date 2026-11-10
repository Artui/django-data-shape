from __future__ import annotations

from array import array


class TreePlan:
    """Which earlier row each row hangs from, decided once and answered in O(1).

    Indices rather than keys: the key is what this table's strategy assigns that index."""

    def __init__(self, parents: array[int]) -> None:
        self._parents = parents

    def parent_for(self, row: int) -> int | None:
        """The index of this row's parent, or None for a root."""
        parent = self._parents[row]
        return None if parent < 0 else parent
