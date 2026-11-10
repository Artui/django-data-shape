from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence


class ChildrenPerParent(Mapping[object, int]):
    """The realised fan-out: one child count per parent key.

    The inversion a ``FanOut`` exists to make possible, in the only coordinate a caller holds."""

    def __init__(self, parents: Sequence[object], sizes: Sequence[int], null_share: float) -> None:
        # Zipped strictly: a length mismatch would mean the sizes came from other parents.
        self._counts = dict(zip(parents, sizes, strict=True))
        self._null_share = null_share
        self._ranked = tuple(sorted(self._counts.items(), key=lambda item: item[1], reverse=True))
        self._childless = tuple(parent for parent, count in self._counts.items() if count == 0)

    def __getitem__(self, parent: object) -> int:
        return self._counts[parent]

    def __iter__(self) -> Iterator[object]:
        return iter(self._counts)

    def __len__(self) -> int:
        return len(self._counts)

    @property
    def null_share(self) -> float:
        """The share of children whose foreign key is null, from the declaration."""
        return self._null_share

    def ranked(self) -> tuple[tuple[object, int], ...]:
        """Every parent and its count, most children first."""
        return self._ranked

    def childless(self) -> tuple[object, ...]:
        """The parents with no children at all, in parent-key order."""
        return self._childless

    def __repr__(self) -> str:
        return (
            f"ChildrenPerParent({len(self._counts)} parents, {sum(self._counts.values())} children)"
        )
