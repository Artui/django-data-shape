"""The one thing every declared value has in common."""

from __future__ import annotations

from typing import Protocol


class Distribution(Protocol):
    """Produces the value of one field for one row.

    Both arguments are supplied to every implementation because the two kinds of
    distribution need different halves: a categorical or numeric one consumes
    ``draw`` and ignores the row, while a monotonic one consumes ``row`` and
    ignores the draw. Passing both keeps the protocol single-method, and a
    single-method protocol is what allows a distribution to be a plain object
    rather than a class hierarchy.

    ``draw`` is uniform in [0, 1) and depends only on the field and the row, so
    an implementation must not carry state between calls. One that did would
    make the same shape produce different data depending on generation order,
    which is the property the placement work in a later release depends on.
    """

    def value(self, row: int, draw: float) -> object: ...
