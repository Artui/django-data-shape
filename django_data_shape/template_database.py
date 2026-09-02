"""Building a shape once per machine, and keeping the result as a template."""

from __future__ import annotations

import hashlib
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.loader import MigrationLoader
from django.db.transaction import TransactionManagementError

from django_data_shape.build import build
from django_data_shape.require_postgres import require_postgres
from django_data_shape.shape import Shape
from django_data_shape.shape_digest import shape_digest
from django_data_shape.version import __version__

# Every template this package makes starts with this, so a machine's caches can
# be listed and dropped as a group. The rest of the name is a digest and nothing
# else -- a readable name would have to come from somewhere, and the only
# candidate is the declaration, which is exactly what the digest already is.
PREFIX = "data_shape_"

# The name a template is built under, and renamed away from once it is complete.
# Existence of the final name is therefore the same claim as "this is finished",
# which is what lets the check below be one row from ``pg_database`` rather than
# a marker inside a database nothing is allowed to connect to.
_PARTIAL = "__partial"

_FORMAT = "django-data-shape template 1"

# 8 bytes: sixteen hexadecimal characters, so the longest name this produces --
# prefix, digest and the partial suffix -- is 36 of PostgreSQL's 63.
_KEY_BYTES = 8


def template_database(shape: Shape, *, using: str = DEFAULT_DB_ALIAS) -> str:
    """Make sure a database holding ``shape`` exists, and return its name.

    The expensive half of the cache, and it runs once per machine rather than
    once per test run. Measured on the two-million-row table this package was
    designed against: generating and ``COPY``-loading it is about nineteen
    seconds, and :func:`~django_data_shape.clone_database.clone_database` turns
    the result into another database in 174 ms. Everything below exists to make
    the first number payable once and the second one the one a suite pays.

    **The name is a digest of everything that decides the contents**, which is
    what makes reuse safe rather than merely fast:

    - the declaration, through
      :func:`~django_data_shape.shape_digest.shape_digest`;
    - the schema it is loaded into -- every migration on disk, and every
      installed model's table, columns, types and nullability, so that a project
      whose apps have no migrations is covered too;
    - ``USE_TZ`` and ``TIME_ZONE``, because every value goes through its field's
      ``get_db_prep_save`` and a datetime column lands somewhere else under a
      different one;
    - this package's own version, because a release that changes how a
      distribution draws changes the rows without changing the declaration.

    Change any of them and the name changes, so the old database is simply not
    asked for again. One thing that is **not** covered, stated rather than
    left to be discovered: editing a ``RunSQL`` inside a migration that already
    exists changes the schema while leaving the migration's name and every
    model's fields alone. Drop the template by hand --
    :func:`~django_data_shape.drop_database.drop_database` -- when that happens.

    **What it does not support**, and why:

    - **Anything but PostgreSQL.** ``CREATE DATABASE ... TEMPLATE`` has no
      equivalent elsewhere, and the answer to "is a table set or a database the
      unit of reuse" is a database precisely because that statement exists.
    - **A shape whose declaration cannot be hashed.** A
      :class:`~django_data_shape.derivations.derived.Derived` or a
      :class:`~django_data_shape.keys.key_function.KeyFunction` wraps a callable,
      and hashing code as though it were data is how a cache serves a database
      built from a function that has since been edited. Those shapes raise
      :class:`~django_data_shape.unhashable_shape.UnhashableShape` and are built
      with :func:`~django_data_shape.build.build` instead.
    - **Being called inside a transaction.** Filling the template means pointing
      the connection at another database and closing it, and closing a
      connection inside an atomic block leaves it unusable for the rest of the
      block. This belongs in session setup, before any test has opened one, and
      says so rather than poisoning the connection.
    - **A template on a different server from the database that will clone it.**
      ``CREATE DATABASE ... TEMPLATE`` copies files on one cluster; there is no
      cross-server form, and nothing here pretends otherwise.
    - **Cleaning up after itself.** A template is a cache on a machine, keyed by
      content, so nothing that survives is ever wrong -- only unused. Deleting on
      a guess would mean dropping a database because this package no longer
      recognised its name.

    **Parallel runs are supported, and that is what the advisory lock is for.**
    Under ``pytest-xdist`` every worker asks for the same template at the same
    moment; without a lock each would find it missing and each would build it.
    The lock is taken on the digest, so workers wanting different templates never
    wait on each other, and it is held on the maintenance connection so it is
    released even if the process dies. Cloning is not serialised by anything
    here -- PostgreSQL handles concurrent copies of one source itself.

    **Connections to a finished template are turned off** with ``ALLOW_CONNECTIONS
    false``, because the single failure mode of the whole mechanism is
    PostgreSQL refusing to copy a database somebody is attached to. Turning them
    off is the difference between that being impossible and it being unlikely.
    To look inside one: ``ALTER DATABASE <name> ALLOW_CONNECTIONS true``.
    """
    # Loosely typed for the reason every other backend-specific reach in this
    # package is: ``_nodb_cursor`` is the wrapper's, not the base class's.
    connection: Any = connections[using]
    require_postgres(connection, "Caching a shape as a template database")
    if connection.in_atomic_block:
        raise TransactionManagementError(
            "Caching a shape as a template database points the connection at another database "
            "and closes it, which cannot be done inside an atomic block -- the connection would "
            "be unusable for the rest of it. Call this from test session setup, before anything "
            "has opened a transaction."
        )

    name = f"{PREFIX}{_key(shape, _context())}"
    quote = connection.ops.quote_name
    # A connection that is not attached to any of the databases about to be
    # created, renamed or dropped, and an autocommit one: CREATE DATABASE and
    # ALTER DATABASE cannot run inside a transaction block. It is Django's own
    # mechanism, used here for what its test runner uses it for.
    with connection._nodb_cursor() as cursor:
        # Session-scoped and held for the whole check-then-create, which is the
        # only way the check means anything: two workers that both read "not
        # there" would both build, and the second would fail on a name the first
        # had just taken.
        cursor.execute("SELECT pg_advisory_lock(%s)", [_lock_id(name)])
        try:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name])
            if cursor.fetchone() is None:
                _create(shape, connection, cursor, name, using, quote)
        finally:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [_lock_id(name)])
    return name


def _create(shape: Shape, connection: Any, cursor: Any, name: str, using: str, quote: Any) -> None:
    """Build the shape into a database under a working name, then adopt it.

    The rename is the commit. A database is created as ``<name>__partial``,
    filled, and only then renamed to the name anybody looks for -- so the
    existence check above is a check that a *finished* template is there, and a
    build interrupted halfway leaves something that is never mistaken for one.
    The alternative, a marker written inside the database, cannot be read once
    connections to it are turned off, and turning them off is what keeps the
    clone from failing.

    A failure drops the partial rather than leaving it. Not tidiness: the next
    run would find the name taken and would have to decide whether the thing
    under it was finished, which is the question the rename exists to remove.
    """
    partial = f"{name}{_PARTIAL}"
    # A partial can survive a process killed between the two statements below,
    # and it is never a finished template, so it is always safe to replace.
    cursor.execute(f"DROP DATABASE IF EXISTS {quote(partial)}")
    cursor.execute(f"CREATE DATABASE {quote(partial)}")
    try:
        _fill(shape, connection, partial, using)
    except BaseException:
        cursor.execute(f"DROP DATABASE IF EXISTS {quote(partial)}")
        raise
    cursor.execute(f"ALTER DATABASE {quote(partial)} RENAME TO {quote(name)}")
    cursor.execute(f"ALTER DATABASE {quote(name)} WITH ALLOW_CONNECTIONS false")


def _fill(shape: Shape, connection: Any, database: str, using: str) -> None:
    """Migrate the schema into ``database`` and build the shape there.

    Pointing an existing connection at another database by rewriting
    ``settings_dict["NAME"]`` is what Django's own test runner does to create a
    test database, and it is used here for the same reason: ``migrate`` and this
    package's loader both work through a connection alias, and inventing a
    second alias would mean editing ``settings.DATABASES`` -- which is the same
    mutation with more moving parts. Django hands the wrapper the very dictionary
    from ``settings.DATABASES``, so one assignment moves both views of it.

    ``run_syncdb`` because an app with no migrations has no other way in, and
    those are exactly the apps a project is most likely to have forgotten about.

    The restore is in a ``finally`` because the alternative is a process left
    pointing at a database that is about to be dropped.
    """
    original = connection.settings_dict["NAME"]
    connection.close()
    connection.settings_dict["NAME"] = database
    try:
        call_command("migrate", database=using, run_syncdb=True, interactive=False, verbosity=0)
        build(shape, using=using)
    finally:
        # Closed before the name goes back, so the connection to the template is
        # gone: PostgreSQL refuses to rename or copy a database anything is
        # attached to, and this is the process most likely to be attached.
        connection.close()
        connection.settings_dict["NAME"] = original


def _key(shape: Shape, context: tuple[str, ...]) -> str:
    """The declaration and everything around it that decides what gets built.

    Split from :func:`_context` rather than reading the environment itself, so
    that the composition is a thing a test can vary: a key that quietly stopped
    depending on the schema, the settings or the package version would still
    look exactly like this one from the outside.
    """
    hasher = hashlib.blake2b(digest_size=_KEY_BYTES)
    _absorb(hasher, _FORMAT)
    _absorb(hasher, shape_digest(shape))
    for part in context:
        _absorb(hasher, part)
    return hasher.hexdigest()


def _context() -> tuple[str, ...]:
    """Everything outside the declaration that a built database depends on.

    The package's own version, because a release that changes how a distribution
    draws changes the rows without changing a word of the declaration. The
    schema, because the same shape loaded into two different tables is two
    different databases. And the two settings that decide what a datetime column
    ends up holding, because every value goes through its field's
    ``get_db_prep_save`` on the way in.
    """
    return (
        __version__,
        _schema_digest(
            tuple(apps.get_models()), tuple(sorted(MigrationLoader(None).disk_migrations))
        ),
        str(settings.USE_TZ),
        str(settings.TIME_ZONE),
    )


def _schema_digest(models: tuple[Any, ...], migrations: tuple[tuple[str, str], ...]) -> str:
    """The migrations on disk, and the models as Python currently describes them.

    Both halves are needed and neither is enough. The migration names catch a
    schema change made the ordinary way, including one that adds an index or a
    constraint that no field mentions. The model fields catch an app with no
    migrations at all, where ``run_syncdb`` builds the tables straight from the
    models and a renamed column would otherwise leave the key unmoved.

    Takes both as arguments rather than reading the registry, for the reason
    ``require_postgres`` takes a connection: a digest that reads the world can
    only be checked against the world, and this one has to be checked against
    two worlds that differ.
    """
    hasher = hashlib.blake2b(digest_size=_KEY_BYTES)
    for app_label, migration in sorted(migrations):
        _absorb(hasher, f"{app_label}.{migration}")
    for model in sorted(models, key=lambda model: str(model._meta.label)):
        _absorb(hasher, f"{model._meta.label}.{model._meta.db_table}")
        for field in model._meta.concrete_fields:
            _absorb(hasher, f"{field.name}|{field.column}|{field.get_internal_type()}|{field.null}")
    return hasher.hexdigest()


def _absorb(hasher: hashlib.blake2b, part: str) -> None:
    """One string, length-prefixed so two parts cannot run together into a third."""
    encoded = part.encode()
    hasher.update(len(encoded).to_bytes(8, "big"))
    hasher.update(encoded)


def _lock_id(name: str) -> int:
    """A signed 64-bit advisory lock id, which is the only shape PostgreSQL takes.

    Derived from the template's name rather than from a constant, so two shapes
    being cached at the same moment do not wait on each other. Advisory locks
    share one namespace across the whole cluster, which is why the name rather
    than the digest alone goes in: another package's lock on the same number
    would block this one for reasons nobody could trace.
    """
    return int.from_bytes(
        hashlib.blake2b(name.encode(), digest_size=8).digest(), "big", signed=True
    )
