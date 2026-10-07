"""Raised when a template cannot start from the base database it was given."""

from __future__ import annotations


class UnusableBase(Exception):
    """A base database cannot be started from, or holds a history migrating cannot repair.

    Cannot be started from: it is missing, it refuses connections, it is a
    template this package made, or its name holds a double quote, which
    Django's quoting cannot carry intact. A history migrating cannot repair: it
    is ahead of the migrations on disk; it has applied part of a squash whose
    remaining replaced migrations are gone from disk, so neither would run; or
    it has no ``django_migrations`` table yet holds the tables of an app with
    migrations, which ``migrate`` would try to create again; or something in it
    depends on a table of an app without migrations, which stops that table
    being dropped from the copy and made again from its model.

    Its own type rather than :class:`~django_data_shape.declaration.invalid_shape.InvalidShape`
    because nothing is wrong with the declaration: the same shape builds from
    empty, and from any base this checkout can migrate. What is wrong is the
    database it was asked to start from.

    Raised only where migrating cannot repair the base. One behind the disk is
    migrated forward in the copy, because migrating forward ends at this
    checkout's schema for the apps with migrations, whatever prefix of their
    history the base holds; the tables of the apps without them are rebuilt
    from the models. One ahead of it cannot be repaired from this checkout at
    all -- the migrations that would undo it are not here -- and accepting it
    would build a template whose schema the cache key does not describe.
    """
