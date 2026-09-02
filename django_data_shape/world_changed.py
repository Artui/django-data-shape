"""Raised when the database no longer holds the world a declaration describes."""

from __future__ import annotations


class WorldChanged(Exception):
    """The rows a question is being asked about are not the rows that were built.

    Its own type because it is the opposite failure from
    :class:`~django_data_shape.invalid_shape.InvalidShape`: the declaration is
    fine and the answer would be arithmetically correct. It would simply be an
    answer about a different database from the one the caller is looking at.

    Raised by :func:`~django_data_shape.fan_out_sizes.fan_out_sizes`, which
    recomputes a fan-out's partition rather than aggregating the child table.
    That recomputation takes one thing from the database -- the parent keys --
    so a parent table that has gained or lost rows since the build produces a
    partition that never existed. Every number would look plausible and every
    one of them would be wrong, which is precisely the class of failure this
    package refuses to ship: a shaped database that quietly means something
    other than what it says.
    """
