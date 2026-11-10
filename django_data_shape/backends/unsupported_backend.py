from __future__ import annotations


class UnsupportedBackend(Exception):
    """The backend cannot take the operation; the declaration itself is valid.

    Not an InvalidShape: the same shape would build against PostgreSQL."""
