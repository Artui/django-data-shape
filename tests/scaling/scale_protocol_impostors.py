from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from django_data_shape import ScaleProtocol


# Keyword-only, so it cannot be called as ``world(10)``.
@contextmanager
def keyword_only(*, factor: int) -> Generator[int]:
    yield factor


# Makes a world and hands back a number rather than a block to run inside.
def not_a_context_manager(factor: int, /) -> int:
    return factor


keyword_only_is_not_one: ScaleProtocol = keyword_only
not_a_context_manager_is_not_one: ScaleProtocol = not_a_context_manager
