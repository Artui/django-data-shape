from __future__ import annotations


class UnusableBase(Exception):
    """A base database cannot be started from, or holds a history migrating cannot repair.

    Not InvalidShape: the same shape builds from empty, so the base is what is wrong."""
