from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol


class ScaleProtocol(Protocol):
    """Make the world be at ``factor``, then let the caller run its block.

    The seam a consumer's growth assertion runs through, one call per scale factor."""

    def __call__(self, factor: int, /) -> AbstractContextManager[int | None]: ...
