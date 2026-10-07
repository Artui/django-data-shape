"""Raised when a template cannot start from the base database it was given."""

from __future__ import annotations


class UnusableBase(Exception):
    """A base database is missing, or its migrations are not the ones on disk.

    Its own type rather than :class:`~django_data_shape.declaration.invalid_shape.InvalidShape`
    because nothing is wrong with the declaration: the same shape builds from
    empty, and from any base that matches. What is wrong is the database it was
    asked to start from.

    Raised rather than repaired. A base behind the disk could be migrated
    forward, and that is exactly what this refuses to do: replaying a long
    history is the cost a base exists to skip, and a template that quietly paid
    it would make every added migration a surprise of several minutes. A base
    ahead of the disk cannot be repaired at all from this checkout. And either
    one accepted as it stands would break the cache key, which names a template
    by the migrations on disk -- a claim that only describes the base's schema
    when the two agree.
    """
