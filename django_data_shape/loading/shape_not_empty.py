from __future__ import annotations


class ShapeNotEmpty(Exception):
    """The destination table is not empty, so the assigned keys would collide.

    Its own type, since the remedy is specific: empty the table, then build again."""
