from __future__ import annotations


class WorldChanged(Exception):
    """The rows a question is being asked about are not the rows that were built.

    Its own type: the declaration is fine, and the database is not what it was built to be."""
