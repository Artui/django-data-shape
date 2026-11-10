from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class Categorical(Protocol):
    """A distribution over a known set of values, with a known share for each.

    The fourth opt-in protocol, beside ``Bounded``."""

    def shares(self) -> Mapping[object, float]: ...
