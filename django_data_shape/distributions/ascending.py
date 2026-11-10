from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Ascending(Protocol):
    """A distribution that can say whether its values rise with the row index.

    The fifth opt-in protocol, and the narrowest: it exists for ``PerParent``'s ``order_by``."""

    def is_ascending(self) -> bool: ...
