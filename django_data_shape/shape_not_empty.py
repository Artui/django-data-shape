"""Raised when a table already holds rows a build would collide with."""

from __future__ import annotations


class ShapeNotEmpty(Exception):
    """The destination table is not empty, so the assigned keys would collide.

    Its own type rather than a reused one because the caller's remedy is
    specific and nothing else in this package shares it: empty the table, then
    build. Raised before any row is written, so a build that fails this way has
    changed nothing.

    The alternative was letting the database report it, which it did -- as a
    unique-violation naming an index. That says what went wrong at the storage
    layer and nothing about what the caller did or what to do instead.
    """
