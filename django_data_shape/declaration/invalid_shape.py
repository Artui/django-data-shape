from __future__ import annotations


class InvalidShape(Exception):
    """A shape declaration is contradictory, incomplete or unsatisfiable.

    Its own type, raised as early as the contradiction can be seen, not at load time."""
