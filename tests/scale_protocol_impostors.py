"""Implementations that must **not** satisfy the scale protocol.

The negative control for ``scale_protocol_consumers.py``. A type-level test whose
checker never actually read the file would pass by finding nothing, which is the
failure mode of every test that discovers its own inputs -- so one file has to
fail for the other one's passing to mean anything.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from django_data_shape import ScaleProtocol


# Keyword-only, so it cannot be called as ``world(10)``. The protocol takes its
# factor positionally on purpose, and this is the mistake that makes the
# difference visible.
@contextmanager
def keyword_only(*, factor: int) -> Iterator[int]:
    yield factor


# Makes a world and hands back a number rather than a block to run inside. A
# world with no scope is one nothing takes down.
def not_a_context_manager(factor: int, /) -> int:
    return factor


keyword_only_is_not_one: ScaleProtocol = keyword_only
not_a_context_manager_is_not_one: ScaleProtocol = not_a_context_manager
