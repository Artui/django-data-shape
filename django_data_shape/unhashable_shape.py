"""Raised when a declaration cannot be reduced to a cache key."""

from __future__ import annotations


class UnhashableShape(Exception):
    """A shape holds something whose contribution to the data cannot be read.

    Its own type rather than :class:`~django_data_shape.invalid_shape.InvalidShape`
    because nothing is wrong with the declaration: it builds, it is reproducible,
    and every row it produces is correct. Only one thing cannot be done with it,
    and that is deciding whether a database built from it earlier is a database
    built from *this* one.

    Raised rather than answered with a digest that leaves the unreadable part
    out. That is the whole point: a cache key which ignores something is a key
    that stays the same when the data changes, and the failure it produces is a
    test suite running against a database nobody asked for -- silently, and in
    the direction that looks like everything is working. A refusal costs a build.
    """
