from __future__ import annotations


class UnhashableShape(Exception):
    """A shape holds something whose contribution to the data cannot be read.

    Raised rather than digested around, since a key that ignores data stays stale."""
