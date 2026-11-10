from __future__ import annotations

import math
from collections import defaultdict

from django_data_shape.utils import draw


class PairedPlan:
    """Distinct partners inside every group, chosen once and read per row.

    Partners are allocated across weight bands, then strided within a band: nothing is drawn."""

    def __init__(
        self,
        keys: list[int],
        sizes: list[int],
        weights: list[float],
        bands: list[int],
        stream: int,
    ) -> None:
        self._keys = keys
        self._sizes = sizes
        # Three streams, not one stream plus offset: an offset ties draws that must be independent.
        self._offsets = stream
        self._strides = stream ^ 0x9E3779B97F4A7C15
        self._starts = stream ^ 0xC2B2AE3D27D4EB4F
        self._members: dict[int, list[int]] = defaultdict(list)
        for index, band in enumerate(bands):
            self._members[band].append(index)
        self._bands = sorted(self._members)
        self._mass = {band: sum(weights[i] for i in self._members[band]) for band in self._bands}
        self._total = sum(self._mass.values())
        self._chosen: list[list[int]] = [self._for_group(g, s) for g, s in enumerate(sizes)]

    def partner_for(self, group: int, position: int) -> int:
        """The key for the ``position``-th row of ``group``."""
        return self._keys[self._chosen[group][position]]

    def sizes(self) -> list[int]:
        """How many rows each group holds, which is the fan-out's own partition."""
        return list(self._sizes)

    def _for_group(self, group: int, size: int) -> list[int]:
        chosen: list[int] = []
        for band, wanted in self._allocate(group, size):
            people = self._members[band]
            modulus = len(people)
            # Each (group, band, purpose) gets its own draw index: arithmetic mixing collides.
            slot = group * len(self._bands) + self._bands.index(band)
            stride = _stride(modulus, draw(self._strides, slot))
            start = int(draw(self._starts, slot) * modulus)
            chosen.extend(people[(start + step * stride) % modulus] for step in range(wanted))
        return chosen

    def _allocate(self, group: int, size: int) -> list[tuple[int, int]]:
        """How many partners this group takes from each band. Sums to ``size``."""
        offset = draw(self._offsets, group)
        running = 0.0
        previous = 0
        wanted: dict[int, int] = {}
        for band in self._bands:
            running += size * self._mass[band] / self._total
            boundary = math.floor(running - offset) + 1
            wanted[band] = max(0, boundary - previous)
            previous = boundary
        spill = 0
        for band in self._bands:
            capacity = len(self._members[band])
            if wanted[band] > capacity:
                spill += wanted[band] - capacity
                wanted[band] = capacity
        for band in self._bands:
            if not spill:
                break
            room = len(self._members[band]) - wanted[band]
            taken = min(room, spill)
            wanted[band] += taken
            spill -= taken
        return [(band, count) for band, count in wanted.items() if count]


def _stride(modulus: int, drawn: float) -> int:
    """A step coprime with ``modulus``, so stepping by it is a bijection."""
    if modulus <= 2:
        return 1
    stride = 1 + int(drawn * (modulus - 1))
    while math.gcd(stride, modulus) != 1:
        stride = stride % (modulus - 1) + 1
    return stride
