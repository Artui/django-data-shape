"""A tree resolved into one parent row index per row."""

from __future__ import annotations

from array import array


class TreePlan:
    """Which earlier row each row hangs from, decided once and answered in O(1).

    Indices rather than keys, on purpose: the parent's key is the key this
    table's own strategy assigns to that index, and turning one into the other
    belongs where every other key of the row is made. ``-1`` marks a root,
    because an ``array`` of machine integers holds a million parents in eight
    megabytes where a list of ``int | None`` would take several times that.
    """

    def __init__(self, parents: array[int]) -> None:
        self._parents = parents

    def parent_for(self, row: int) -> int | None:
        """The index of this row's parent, or None for a root."""
        parent = self._parents[row]
        return None if parent < 0 else parent
