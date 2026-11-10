from __future__ import annotations

from enum import Enum


class Scope(Enum):
    """The one thing that distinguishes one derivation from another.

    What varies between kinds is not how the computation runs but where its inputs live."""

    ROW = "row"
    PARENT = "parent"
    RANK = "rank"
    GROUP = "group"
