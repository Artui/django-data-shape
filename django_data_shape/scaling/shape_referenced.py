from __future__ import annotations


class ShapeReferenced(Exception):
    """Rows the world did not make reference rows it would have to remove.

    A scaled world empties the tables its shape declares, and removes only their rows."""
