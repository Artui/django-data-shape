"""Helpers used across more than one module."""

from __future__ import annotations

import hashlib

_MASK64 = (1 << 64) - 1
_GOLDEN = 0x9E3779B97F4A7C15


def field_stream(seed: int, table: str, field: str) -> int:
    """A stable 64-bit stream id for one table's one field.

    Derived once per field rather than per row, so the cost of a real hash is
    paid a handful of times instead of millions. ``hash()`` is deliberately not
    used: it is salted per interpreter run, which would make a seeded shape
    reproduce only within a single process.
    """
    digest = hashlib.blake2b(f"{table}.{field}".encode(), digest_size=8).digest()
    return (int.from_bytes(digest, "big") ^ seed) & _MASK64


def draw(stream: int, row: int) -> float:
    """A uniform float in [0, 1) for one field of one row.

    Deterministic in ``(stream, row)`` alone, which is the property the whole
    design rests on: a value does not depend on how many rows were generated
    before it, so rows can later be emitted in an order different from the one
    they were assigned in. That separation is what lets physical placement be
    declared without buffering a group in memory.

    SplitMix64's finalizer rather than ``random.Random``: seeding a Mersenne
    Twister per value costs far more than the value is worth at these row
    counts, and this needs no sequential state to be reproducible.
    """
    z = (stream + (row + 1) * _GOLDEN) & _MASK64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
    z = z ^ (z >> 31)
    return z / 18446744073709551616.0
