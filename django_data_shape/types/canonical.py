from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Canonical(Protocol):
    """A declaration that can describe itself as plain, comparable data.

    The third opt-in protocol in this package, for the same reason as the other two."""

    def canonical(self) -> object: ...
