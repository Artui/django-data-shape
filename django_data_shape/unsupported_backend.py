"""Raised when the connection cannot do what was asked of it."""

from __future__ import annotations


class UnsupportedBackend(Exception):
    """The database backend cannot support the operation requested.

    Separate from :class:`~django_data_shape.invalid_shape.InvalidShape`
    because nothing is wrong with the declaration: the same shape is valid, and
    would build, against Postgres. Only the destination is unsuitable.

    Raised rather than warned, and never quietly degraded to a slower path. The
    whole claim of this package is that the loaded database is one the planner
    can reason about; a backend without ``COPY`` or column statistics cannot
    produce that, and silently producing something else would be the failure
    mode the package was written to expose.
    """
