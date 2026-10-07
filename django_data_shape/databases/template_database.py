"""Building a shape once per machine, and keeping the result as a template."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.transaction import TransactionManagementError

from django_data_shape.backends.require_postgres import require_postgres
from django_data_shape.databases.shape_digest import shape_digest
from django_data_shape.databases.unusable_base import UnusableBase
from django_data_shape.declaration.shape import Shape
from django_data_shape.loading.build import build
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

# How many differing migrations a refused base's message names. Enough to say
# which app and roughly how far, without pasting a whole history into a
# traceback when the base is hundreds of migrations out.
_EXAMPLES = 3

# 8 bytes: sixteen hexadecimal characters, so the longest name this produces --
# prefix, digest and the partial suffix -- is 36 of PostgreSQL's 63.
_KEY_BYTES = 8


def template_database(
    shape: Shape, *, using: str = DEFAULT_DB_ALIAS, base: str | None = None
) -> str:
    """Make sure a database holding ``shape`` exists, and return its name.

    The expensive half of the cache, and it runs once per machine rather than
    once per test run. Measured on the two-million-row table this package was
    designed against: generating and ``COPY``-loading it is about nineteen
    seconds, and :func:`~django_data_shape.databases.clone_database.clone_database` turns
    the result into another database in 174 ms. Everything below exists to make
    the first number payable once and the second one the one a suite pays.

    **The name is a digest of everything that decides the contents**, which is
    what makes reuse safe rather than merely fast:

    - the declaration, through
      :func:`~django_data_shape.databases.shape_digest.shape_digest`;
    - the schema it is loaded into -- every migration on disk, and every
      installed model's table, columns, types and nullability, so that a project
      whose apps have no migrations is covered too;
    - ``USE_TZ`` and ``TIME_ZONE``, because every value goes through its field's
      ``get_db_prep_save`` and a datetime column lands somewhere else under a
      different one;
    - this package's own version, because a release that changes how a
      distribution draws changes the rows without changing the declaration;
    - with a ``base``, the base's name and its database oid, because its rows
      become the template's rows. Dropping and recreating the base gives it a
      new oid, so a refreshed base is a new template.

    Change any of them and the name changes, so the old database is simply not
    asked for again. Two things that are **not** covered, stated rather than
    left to be discovered: editing a ``RunSQL`` inside a migration that already
    exists changes the schema while leaving the migration's name and every
    model's fields alone; and editing the rows a base holds in place, with no
    migration and no recreate, changes what a new template would copy while
    leaving the base's name and oid alone. Drop the template by hand --
    :func:`~django_data_shape.databases.drop_database.drop_database` -- when either happens.

    **Starting from a migrated base.** With ``base`` naming a database that is
    already migrated, the template starts as a copy of it rather than as an
    empty database that ``migrate`` replays the whole history into -- which on
    a project with hundreds of migrations is most of the cost, and repaid by
    every template a change to the declaration makes. ``migrate`` still runs
    over the copy, finding nothing to apply, so an app with no migrations gets
    its tables and ``post_migrate`` fires exactly as it does from empty.

    **A base whose migrations are not the ones on disk is refused, never
    migrated forward**, raising
    :class:`~django_data_shape.databases.unusable_base.UnusableBase` before
    anything is created. That is what makes the key sound: the migrations on
    disk are already part of it, and refusing every other base is what lets
    them describe the base's schema too. Behind -- a migration on disk not yet
    applied to it -- and ahead -- an applied migration no migration on disk is
    or replaces -- are refused separately, each naming a few of the migrations
    that differ, and so is a base that does not exist. The check runs on every
    call, a cache hit included, because the oid it reads is part of the name.

    Whatever else the base holds becomes template content. Rows in a table the
    shape declares are refused by :func:`~django_data_shape.loading.build.build`'s
    emptiness check as they would be anywhere, except in a table whose keys are
    :class:`~django_data_shape.keys.disjoint.Disjoint`, which is exempt from it.

    **What it does not support**, and why:

    - **Anything but PostgreSQL.** ``CREATE DATABASE ... TEMPLATE`` has no
      equivalent elsewhere, and the answer to "is a table set or a database the
      unit of reuse" is a database precisely because that statement exists.
    - **A shape whose declaration cannot be hashed.** A
      :class:`~django_data_shape.derivations.derived.Derived` or a
      :class:`~django_data_shape.keys.key_function.KeyFunction` wraps a callable,
      and hashing code as though it were data is how a cache serves a database
      built from a function that has since been edited. Those shapes raise
      :class:`~django_data_shape.databases.unhashable_shape.UnhashableShape` and are built
      with :func:`~django_data_shape.loading.build.build` instead.
    - **Being called inside a transaction.** Filling the template means pointing
      the connection at another database and closing it, and closing a
      connection inside an atomic block leaves it unusable for the rest of the
      block. This belongs in session setup, before any test has opened one, and
      says so rather than poisoning the connection.
    - **A template on a different server from the database that will clone it.**
      ``CREATE DATABASE ... TEMPLATE`` copies files on one cluster; there is no
      cross-server form, and nothing here pretends otherwise. A ``base`` is
      bound by the same rule, for the same reason.
    - **A base something is attached to while the template is copied.** The
      same rule :func:`~django_data_shape.databases.clone_database.clone_database`
      documents: PostgreSQL refuses to copy a database with other sessions on
      it. This process's own connection is closed before the copy, so a project
      whose test database *is* the base can pass it as one; a connection another
      process holds is not something this can close.
    - **Migrating a base.** A base that is behind or ahead of the migrations on
      disk is refused with the remedy rather than brought up to date, for the
      reasons above.
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

    # The base is read before anything else touches the server, and on a cache
    # hit as well as a miss: its oid is part of the key, so there is no name to
    # look for until it has been read, and refusing a stale base costs one
    # catalogue row and one migration-table read.
    context = _context() if base is None else (*_context(), *_base_context(connection, base))
    name = f"{PREFIX}{_key(shape, context)}"
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
                _create(shape, connection, cursor, name, using, quote, base)
        finally:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [_lock_id(name)])
    return name


def _create(
    shape: Shape,
    connection: Any,
    cursor: Any,
    name: str,
    using: str,
    quote: Any,
    base: str | None,
) -> None:
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

    With a ``base`` the partial starts as a copy of it, with no ``STRATEGY``
    clause: a base is schema-sized, so the server's default costs nothing worth
    choosing between, and the copy that is big -- the finished template into a
    test database -- is :func:`~django_data_shape.databases.clone_database.clone_database`'s,
    which already defaults to ``file_copy``.
    """
    partial = f"{name}{_PARTIAL}"
    # A partial can survive a process killed between the two statements below,
    # and it is never a finished template, so it is always safe to replace.
    cursor.execute(f"DROP DATABASE IF EXISTS {quote(partial)}")
    template = "" if base is None else f" TEMPLATE {quote(base)}"
    cursor.execute(f"CREATE DATABASE {quote(partial)}{template}")
    try:
        _fill(shape, connection, partial, using)
    except BaseException:
        cursor.execute(f"DROP DATABASE IF EXISTS {quote(partial)}")
        raise
    cursor.execute(f"ALTER DATABASE {quote(partial)} RENAME TO {quote(name)}")
    cursor.execute(f"ALTER DATABASE {quote(name)} WITH ALLOW_CONNECTIONS false")


def _fill(shape: Shape, connection: Any, database: str, using: str) -> None:
    """Migrate the schema into ``database`` and build the shape there.

    A database copied from an accepted base already has every migration on disk
    applied, so ``migrate`` finds nothing to run -- and is still run, because it
    is also what creates the tables of an app with no migrations and what fires
    ``post_migrate``, so a template from a base is filled exactly as one from
    empty is.

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


def _base_context(connection: Any, base: str) -> tuple[str, str]:
    """Refuse a base the migrations on disk do not describe; else what the key takes from it.

    The name and the database's oid. The oid is the part doing the work: a base
    dropped and recreated -- the usual way one restored from a schema dump is
    refreshed -- gets a new one, so the template built from the old base is
    simply not asked for again. A rename re-keys too, which costs a build and is
    otherwise harmless. The base's *migrations* are not hashed, because they
    are refused below unless they are the ones on disk, which the key already
    holds.

    Read through ``_nodb_cursor`` rather than the connection itself, because the
    connection may be the very one that is about to be pointed at the base.
    """
    with connection._nodb_cursor() as cursor:
        cursor.execute("SELECT oid FROM pg_database WHERE datname = %s", [base])
        row = cursor.fetchone()
    if row is None:
        raise UnusableBase(
            f"The base database {base!r} does not exist, so no template can start from it. "
            "Create and migrate it, or pass base=None to build the template from an empty "
            "database."
        )
    behind, ahead = _migration_state(connection, base)
    if behind:
        raise UnusableBase(
            f"The base database {base!r} is behind the migrations on disk: {len(behind)} "
            f"of them are not applied to it, {_examples(behind)}. A template is never "
            "migrated forward from its base -- replaying the history is what a base exists "
            "to skip, and the cache key names the schema by the migrations on disk. Migrate "
            "the base, or recreate it, and ask again."
        )
    if ahead:
        raise UnusableBase(
            f"The base database {base!r} is ahead of the migrations on disk: it records "
            f"{len(ahead)} applied migration(s) that no migration on disk is or replaces, "
            f"{_examples(ahead)}. Its schema is then something the migrations on disk do "
            "not describe, and they are what the cache key says a template holds. Migrate "
            "the base back from a checkout that has those migrations, or recreate it. If "
            "they were deleted on purpose -- a squash whose replaces attribute has since "
            "been removed, or an app no longer installed -- delete their rows from the "
            "base's django_migrations table."
        )
    return base, str(row[0])


def _migration_state(
    connection: Any, base: str
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """What ``base`` has yet to apply, and what it has applied that the disk lacks.

    Behind is ``migrate``'s own plan over every leaf, read while connected to
    the base, rather than a comparison of names: the executor already knows
    when a squash counts as applied and when its replaced migrations do, and a
    hand-rolled version of that is the kind of code that is right until a
    project squashes.

    The connection is pointed at the base the way :func:`_fill` points it at a
    partial, and closed before the name goes back, which is also what lets the
    base be copied straight afterwards: PostgreSQL refuses to copy a database
    anything is attached to, and nothing between here and the copy opens this
    connection again. Safe to close because ``template_database`` has already
    refused to run inside an atomic block.
    """
    original = connection.settings_dict["NAME"]
    connection.close()
    connection.settings_dict["NAME"] = base
    try:
        executor = MigrationExecutor(connection)
        loader = executor.loader
        behind = [
            (migration.app_label, migration.name)
            for migration, _backwards in executor.migration_plan(loader.graph.leaf_nodes())
        ]
        ahead = _ahead_of_disk(
            loader.applied_migrations,
            disk=loader.disk_migrations,
            replaced=[
                (app_label, name)
                for migration in loader.disk_migrations.values()
                for app_label, name in migration.replaces
            ],
        )
    finally:
        connection.close()
        connection.settings_dict["NAME"] = original
    return behind, ahead


def _ahead_of_disk(
    applied: Iterable[tuple[str, str]],
    *,
    disk: Iterable[tuple[str, str]],
    replaced: Iterable[tuple[str, str]],
) -> list[tuple[str, str]]:
    """The applied migrations that are neither on disk nor replaced by one that is.

    The replaced exclusion is what keeps an ordinary squash from refusing a good
    base: once the replaced files are deleted, their records stay behind in
    ``django_migrations``, legitimately, and the squash on disk names them in
    its ``replaces``.

    Takes its inputs as arguments rather than reading a loader, for the reason
    :func:`_schema_digest` does: the exclusion needs a squashed migration on disk
    to reach through a real base, and as arithmetic it needs nothing. Each
    condition is held by a test, because a branch gate sees the comprehension's
    filter as one arc and would stay green with either deleted:

    - not on disk: ``test_a_migration_on_disk_is_not_ahead_of_it``, and every
      test that builds from a base;
    - not replaced: ``test_unless_a_migration_on_disk_replaces_it``, and
      ``test_a_squash_whose_replaced_files_were_deleted_does_not_make_a_base_ahead``,
      which also holds that the replaced names are read off the disk at all.
    """
    on_disk = set(disk)
    excused = set(replaced)
    return sorted(key for key in applied if key not in on_disk and key not in excused)


def _examples(migrations: list[tuple[str, str]]) -> str:
    """A few migrations by name, and how many more there are."""
    named = ", ".join(f"{app_label}.{name}" for app_label, name in migrations[:_EXAMPLES])
    rest = len(migrations) - _EXAMPLES
    return f"for example {named}" + (f" and {rest} more" if rest > 0 else "")


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
