"""Building a shape once per machine, and keeping the result as a template."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

import django
from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.db import DEFAULT_DB_ALIAS, InternalError, connections, router
from django.db.migrations.loader import MigrationLoader
from django.db.transaction import TransactionManagementError

from django_data_shape.backends.require_postgres import require_postgres
from django_data_shape.databases.require_quotable_name import require_quotable_name
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

# How many migrations or tables a refused base's message names. Enough to say
# which app and roughly how far, without pasting a whole history into a
# traceback when a base is dozens of migrations out.
_EXAMPLES = 3

# 8 bytes: sixteen hexadecimal characters, so the longest name this produces --
# prefix, digest and the partial suffix -- is 36 of PostgreSQL's 63.
_KEY_BYTES = 8

# A name this package generated, built from the three parts every such name is
# built from -- the prefix, the key's hexadecimal digest at its fixed length, and
# the partial suffix or nothing -- so that refusing a template as a base refuses
# exactly those. The prefix alone would also refuse a user's own
# ``data_shape_userdb``, with a message calling it something this package made.
_GENERATED = re.compile(
    rf"{re.escape(PREFIX)}[0-9a-f]{{{_KEY_BYTES * 2}}}(?:{re.escape(_PARTIAL)})?"
)

# Which of the tables a copy rebuilds the current role may not drop, and who
# owns each. PostgreSQL lets a table be dropped by its owner, by the owner of its
# schema, or by a superuser, and checks ownership as "has the privileges of":
# membership that inherits them, superuser included. pg_has_role with USAGE is
# that same test, which MEMBER is not -- a member without INHERIT has to SET
# ROLE first, and DROP TABLE does not. The names are bound quoted, because
# regclass input is read as an identifier. :func:`_drop_unmigrated_tables`
# names the test holding each condition.
_UNDROPPABLE = """
SELECT c.relname, pg_get_userbyid(c.relowner), current_user
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE c.oid = ANY (%s::regclass[])
  AND NOT pg_has_role(c.relowner, 'USAGE')
  AND NOT pg_has_role(n.nspowner, 'USAGE')
"""


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
    - with a ``base``, the base's name and its database oid, because the rows
      it holds in the tables of apps with migrations become the template's
      rows. Dropping and recreating the base gives it a new oid, so a refreshed
      base is a new template.

    Change any of them and the name changes, so the old database is simply not
    asked for again. Two things that are **not** covered, stated rather than
    left to be discovered: editing a ``RunSQL`` inside a migration that already
    exists changes the schema while leaving the migration's name and every
    model's fields alone; and changing the rows a base holds -- by hand, or by
    migrating the base, neither of which moves its name or oid -- changes what
    a new template would copy while the key stays where it was. Drop the
    template by hand --
    :func:`~django_data_shape.databases.drop_database.drop_database` -- when either happens.
    With a base, an edited migration goes one step further, and it is a case
    nothing could detect: a migration regenerated under a name
    the base has already applied is read by ``migrate`` as applied and skipped,
    so the copy keeps the version the base ran, and a template rebuilt from the
    same base would keep it again. Recreate the base, which gives it a new oid
    and so a new template. And one more a base brings that nothing detects,
    where recreating the base does not help: SQL that an applied migration of
    an app with migrations ran against a table of an app without them -- an
    index, a trigger, a policy, a grant or a comment -- is lost when the copy
    rebuilds that table, because the migration counts as applied and never
    runs again, and a recreated base has applied it too. Build that template
    from empty, with ``base=None``, where ``run_syncdb`` makes the table before
    the migration runs.

    **Starting from a migrated base.** With ``base`` naming a database that is
    already migrated, the template starts as a copy of it rather than as an
    empty database that ``migrate`` replays the whole history into -- which on
    a project with hundreds of migrations is most of the cost, and repaid by
    every template a change to the declaration makes. The tables ``migrate``
    builds for apps without migrations are rebuilt from the models, and
    everything else the base holds is carried as it is. An app with migrations
    is brought forward by its history: ``migrate`` runs over the copy and
    applies whatever the base has yet to. An app without them has no history to
    bring forward, so the tables ``run_syncdb`` makes for it are dropped from
    the copy first and made again from the current models, as in an empty
    database -- a table the base made from an older model is not kept under a
    key naming the new one, and the base's rows in those tables do not carry
    over. Everything else is carried: the rows in the tables of apps with
    migrations, an unmanaged model's table, and the tables of an app that is
    no longer installed. ``post_migrate`` fires as it does from empty.

    **A base behind the migrations on disk is migrated forward; only a history
    migrating cannot repair is refused.** Migrating forward always ends at this
    checkout's schema for the apps with migrations, whatever prefix of their
    history the base holds, and the apps without them are rebuilt, so the key
    stays sound as it is -- everything listed above, the base's name and oid
    included -- without the base's applied migrations entering it. Migrating
    the base itself forward later leaves its oid alone, so it asks for the same
    name, and the template under that name is the one the copy's own forward
    migration already gave. Django's own PostgreSQL ``TEST: {"TEMPLATE": ...}``
    setting behaves the same way. A base far behind pays its ``migrate`` once
    per key, which is never more than building from empty pays, and accepting
    it is what keeps the commonest base -- one migration behind, because a
    branch added one -- from being refused at all.

    Ahead -- an applied migration of an installed app that no migration on disk
    is or replaces -- raises
    :class:`~django_data_shape.databases.unusable_base.UnusableBase` before
    anything is created, naming a few of those migrations and both remedies. So
    do two histories ``migrate`` would mishandle rather than refuse: a squash
    the base has applied only in part, when a replaced migration it has yet to
    apply is gone from disk -- Django then runs neither the squash nor the rest
    of what it replaces, and the copy would lack them silently -- and a base
    holding the tables of an app with a migration on disk that it records no
    applied migration for, which ``migrate`` would try to create again: a base with no
    ``django_migrations`` table, one restored from ``pg_dump --schema-only``,
    which brings that table back empty, or one made before an app had
    migrations. So does a base
    that does not exist, one that refuses connections, a template this package
    made or the partial of one, and a name holding a double quote, which
    Django's quoting cannot carry intact. Two more are raised from the copy
    rather than before it, because only the copy can find them, and the partial
    goes with either refusal: something outside the tables being rebuilt that
    depends on one of them -- a foreign key or a view -- stops those tables
    being dropped and made again; and so does a connecting role that may not
    drop them, being neither their owner nor their schema's, nor a member
    inheriting either's privileges, nor a superuser. Only the copy can say
    which, because the role that makes it owns it, and a database's owner holds
    the privileges of ``pg_database_owner``, which owns ``public`` from
    PostgreSQL 15. Of the histories a base can record,
    ahead is the one migrating cannot bring to the checkout's schema: it moves
    only forward, so whatever those migrations did stays in the copy, and a branch
    migration that adds only an index would otherwise build silently and skew
    plan assertions. Rows for an app that is not installed are ignored, because
    they describe nothing the checkout's models use. The check runs on every
    call, a cache hit included, because the oid it reads is part of the name.

    Whatever else the base holds, outside the tables being rebuilt, becomes
    template content. Rows in a table the shape declares
    are refused by :func:`~django_data_shape.loading.build.build`'s
    emptiness check as they would be anywhere, with a message that names the
    base as one place they come from, and the partial is dropped with the
    failure -- except in a table whose keys are
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
    - **A base ahead of the migrations on disk.** It is refused with the
      remedies rather than migrated back, because the migrations that would
      undo it are not in this checkout, for the reasons above.
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
        _fill(shape, connection, partial, using, base)
    except BaseException:
        cursor.execute(f"DROP DATABASE IF EXISTS {quote(partial)}")
        raise
    cursor.execute(f"ALTER DATABASE {quote(partial)} RENAME TO {quote(name)}")
    cursor.execute(f"ALTER DATABASE {quote(name)} WITH ALLOW_CONNECTIONS false")


def _fill(shape: Shape, connection: Any, database: str, using: str, base: str | None) -> None:
    """Migrate the schema into ``database`` and build the shape there.

    In a database copied from a base, the tables ``run_syncdb`` makes are
    rebuilt and everything else is carried as the base has it. Its migrated
    apps are brought forward by their history -- whatever migrations on disk
    the base has yet to apply, none for a base kept up to date. The tables
    ``run_syncdb`` makes for apps with no migrations have no history to bring
    forward, so they are dropped first, and ``run_syncdb`` makes them from the
    current models as it does in an empty database; the base's rows in them do
    not carry over. ``migrate`` is run either way, because it is also what
    fires ``post_migrate``.

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
        if base is not None:
            _drop_unmigrated_tables(connection, base)
        call_command("migrate", database=using, run_syncdb=True, interactive=False, verbosity=0)
        build(shape, using=using)
    finally:
        # Closed before the name goes back, so the connection to the template is
        # gone: PostgreSQL refuses to rename or copy a database anything is
        # attached to, and this is the process most likely to be attached.
        connection.close()
        connection.settings_dict["NAME"] = original


def _drop_unmigrated_tables(connection: Any, base: str) -> None:
    """Drop the copy's tables for installed apps with no migrations.

    ``run_syncdb`` creates a missing table and never alters an existing one, so
    left in place, a table the base made from an older model would survive
    under a key that names the current one -- the key absorbs every model's
    columns, which is exactly what would be stale. Dropped, it is made again
    from the model, as it is in an empty database. The apps are the ones
    ``migrate``'s own loader reads as unmigrated off disk, and the tables the
    ones its ``run_syncdb`` phase reads off their models, as :func:`_tables_of`
    describes, so the set dropped is the set ``run_syncdb`` then makes. What a
    migration of another app did to one of those tables goes with it, and is
    not made again: the migration counts as applied. That is a stated gap
    rather than a refusal, because nothing in the copy says which index or
    trigger a migration made.

    One ``DROP TABLE`` for all of them, so the references between them do not
    order it, and without ``CASCADE``. Anything outside the set that refers to
    one of them -- a foreign key or a view -- would be removed by ``CASCADE``
    silently, ``run_syncdb`` would not put it back, and the template would match
    neither the base nor one built from empty. PostgreSQL refuses the drop
    instead, and the refusal is turned into
    :class:`~django_data_shape.databases.unusable_base.UnusableBase`; the
    partial goes with it, as any failure takes it.

    **A table the connecting role may not drop is refused first**, naming each
    one and its owner, because ``DROP TABLE`` needs ownership and the copy keeps
    every owner the base gave: PostgreSQL's own refusal is a
    ``ProgrammingError`` naming the first such table and no remedy. It is asked
    of the copy rather than of the base, and that is not a matter of
    convenience. Owning the schema is as good as owning the table, the role
    that made the copy owns the copy, and the owner of a database holds the
    privileges of ``pg_database_owner`` -- which owns ``public`` in every
    database made from PostgreSQL 15's own template. So a role owning none of
    those tables may still drop them all from its copy, and only the copy can
    say so; the base belongs to whoever made it. The partial goes with this
    refusal too. Each condition of the query is held by a test of its own,
    because the whole query is one statement to a branch gate:

    - the table's owner:
      ``test_a_role_owning_those_tables_may_rebuild_them_without_owning_their_schema``;
    - the schema's owner: ``test_so_may_one_owning_their_schema_without_owning_them``;
    - privileges rather than membership, ``USAGE`` rather than ``MEMBER``:
      ``test_so_is_one_that_is_a_member_of_their_owner_without_inheriting_its_privileges``;
    - and that what is left is refused:
      ``test_a_base_whose_rebuilt_tables_the_role_cannot_drop_is_refused_naming_their_owner``.

    The ``if`` is held by ``test_migrate_still_runs_over_the_copy``, whose base
    has no such table, because ``DROP TABLE`` with nothing to drop is a syntax
    error.
    """
    tables = _tables_of(connection, MigrationLoader(None).unmigrated_apps)
    if not tables:
        return
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(_UNDROPPABLE, [[quote(table) for table in tables]])
        undroppable = sorted(cursor.fetchall())
    if undroppable:
        role = undroppable[0][2]
        owners = [f"{table} (owned by {owner})" for table, owner, _role in undroppable]
        raise UnusableBase(
            f"The base database {base!r} holds {len(undroppable)} table(s) of apps without "
            f"migrations that the role this connects as, {role!r}, may not drop, "
            f"{_examples(owners)}. A template from a base drops those tables from its copy and "
            "makes them again from their models; PostgreSQL lets only a table's owner, its "
            "schema's owner or a superuser drop one, counting a role that inherits an owner's "
            "privileges as that owner, and the copy keeps the owners the base gave. Connect as "
            "the owner or a member of it that inherits its privileges, reassign the tables in "
            f"the base with ALTER TABLE ... OWNER TO {quote(role)}, or pass base=None to build "
            "from empty."
        )
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE {', '.join(quote(table) for table in tables)}")
    except InternalError as refused:
        # The server's own words name the object in the way, which nothing here
        # could name better; its hint, to add CASCADE, is the one thing not to do.
        reported = " ".join(str(refused).split("HINT:", 1)[0].split())
        raise UnusableBase(
            f"The base database {base!r} holds something that depends on a table of an app "
            "without migrations, so the table cannot be dropped from the copy and made again "
            "from its model, which is how a template from a base gets those tables. "
            f"PostgreSQL reports: {reported}. Drop the reference in the base, or recreate it "
            "without one. If a migration of an app with migrations made it, which Django "
            "allows, no base can be started from until the app it points at has migrations "
            "of its own; pass base=None to build from empty."
        ) from refused


def _base_context(connection: Any, base: str) -> tuple[str, str]:
    """Refuse a base no migration can bring to the disk's schema; else what the key takes.

    The name and the database's oid. The oid is the part doing the work: a base
    dropped and recreated -- the usual way one restored from a dump is
    refreshed -- gets a new one, so the template built from the old base is
    simply not asked for again. A rename re-keys too, which costs a build and is
    otherwise harmless. The base's applied migrations are not hashed: a base
    behind the disk is migrated forward in the copy, which ends at the schema
    the migrations on disk name and the key already holds, and a base ahead of
    it is refused below.

    Read through ``_nodb_cursor`` rather than the connection itself, because the
    connection may be the very one that is about to be pointed at the base.

    Every refusal before the migration read is about the database rather than
    its history, and each one replaces a failure that would otherwise surface
    from somewhere unhelpful. A name holding a double quote is refused before
    anything is read, because Django's ``quote_name`` passes a name that is
    already quoted through as it is and escapes nothing inside it: the lookup
    here would check one database and the copy would read another. A database
    that refuses connections cannot have its migrations read, and the server's
    own refusal would arrive as an ``OperationalError`` from inside Django's
    migration loader. A template this package made refuses connections as
    well, but allowing them is not the remedy there, so it is refused by name
    first, connections allowed or not: it holds a shape's rows under a name
    keyed for that shape, and nothing is ever started from one. The name has
    to be one this package generates, the prefix and a digest of the key's
    length with or without the partial suffix, and not merely share the
    prefix: a user's own database can.
    """
    require_quotable_name(base, "base database", refusal=UnusableBase)
    with connection._nodb_cursor() as cursor:
        cursor.execute("SELECT oid, datallowconn FROM pg_database WHERE datname = %s", [base])
        row = cursor.fetchone()
    if row is None:
        raise UnusableBase(
            f"The base database {base!r} does not exist, so no template can start from it. "
            "Create and migrate it, or pass base=None to build the template from an empty "
            "database."
        )
    if _GENERATED.fullmatch(base):
        raise UnusableBase(
            f"The base database {base!r} is a template this package made, and a template is "
            "never a base: it holds a shape's rows under a name keyed for that shape. Pass the "
            "migrated database the project keeps, or base=None to build from empty."
        )
    if not row[1]:
        raise UnusableBase(
            f"The base database {base!r} does not accept connections, and a base is "
            "connected to before it is copied, to read which migrations it has applied. "
            f"Allow them with ALTER DATABASE {connection.ops.quote_name(base)} WITH "
            "ALLOW_CONNECTIONS true, or pass a different base."
        )
    _refuse_history(connection, base)
    return base, str(row[0])


def _refuse_history(connection: Any, base: str) -> None:
    """Refuse a base whose recorded history no migration can carry to the disk's schema.

    Three cases, read off one loader built while connected to the base:

    - **ahead**, an applied migration that no migration on disk is or replaces,
      as :func:`_ahead_of_disk` reckons it. The message says to prune, and
      names a squash to finish first wherever ``migrate --prune`` would decline
      over one, by the test Django itself makes: a squash that still lists a
      migration the base records as applied and that is gone from disk --
      every app's such migrations up to Django 5.0, and from 5.1 only the
      pruned app's. Finishing a squash means dropping its ``replaces``, which
      breaks the graph while a replaced file is still on disk, so naming one
      that Django would not decline over does harm.
      ``test_a_base_ahead_only_of_a_squash_of_a_squash_says_to_finish_the_squash``
      follows the advice through to a base that builds;
      ``test_a_squash_listing_nothing_prune_would_remove_is_not_named_as_one_to_finish``
      holds both that the squash must list such a migration and that one on
      disk does not count; and
      ``test_a_squash_in_another_app_is_named_only_where_prune_declines_for_it``
      holds which apps' migrations count on each side of 5.1, against what
      ``migrate --prune`` does there;
    - **a squash applied in part** whose replaced migrations the base still
      needs are gone from disk, as :func:`_stranded_squashes` reckons it;
    - **tables with no record of what made them**: an app with migrations
      whose tables the base holds and for which it records no applied
      migration. ``migrate`` would create those tables a second time and fail
      on the first, with ``already exists`` from inside the executor. A base
      with no ``django_migrations`` table is the whole-database form, and
      reads as having applied nothing; a base restored from ``pg_dump
      --schema-only`` is the commonest, because the table comes back with
      none of its rows; and a base made while an app had no migrations is the
      one-app form, once the app gains them. So it is judged per app, which
      ``test_so_is_one_with_no_record_of_a_single_app_whose_tables_it_holds``
      holds, and an empty database, holding no tables, is migrated in full.
      An app is judged only once a migration of its is on disk, which
      ``test_but_an_app_whose_migrations_package_holds_no_migration_is_not``
      holds: an empty migrations package makes an app migrated, which keeps
      ``run_syncdb`` off it, with nothing for ``migrate`` to run, so its tables
      stay as they are and the copy migrates. One history is refused although
      ``migrate`` would accept it: an app whose migrations on disk, applied to
      the base, would create nothing -- a ``0001_initial`` holding only
      ``SeparateDatabaseAndState`` state operations, say. Telling that app
      apart would mean reading what each of its operations does to the
      database, which this check does not. Only those apps' tables count. An
      app without migrations gets its tables
      from ``run_syncdb``, which records nothing, and a project with no
      migrated app at all never makes a ``django_migrations`` table, so a base
      holding only such tables is one ``migrate`` could have made --
      ``test_but_a_table_of_an_app_without_migrations_does_not_count`` holds
      that.

    The applied set is the loader's rather than the raw rows: the loader already
    knows when a squash counts as applied, and a hand-rolled reading of
    ``django_migrations`` is the kind of code that is right until a project
    squashes.

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
        loader = MigrationLoader(connection)
        recorded = {app_label for app_label, _name in loader.applied_migrations}
        # An empty migrations package makes an app migrated, which takes it out
        # of run_syncdb, and gives migrate nothing to run for it, so its tables
        # are left alone: only an app with a migration on disk can fail.
        judged = loader.migrated_apps & {app_label for app_label, _name in loader.disk_migrations}
        unrecorded = {
            app_label: tables
            for app_label in sorted(judged - recorded)
            if (tables := _tables_of(connection, [app_label]))
        }
        ahead = _ahead_of_disk(
            loader.applied_migrations,
            disk=loader.disk_migrations,
            replaced=[
                (app_label, name)
                for migration in loader.disk_migrations.values()
                for app_label, name in migration.replaces
            ],
            installed={app_config.label for app_config in apps.get_app_configs()},
        )
        # The squashes migrate --prune declines over, by Django's own test: one
        # that lists a migration the base records as applied and that is gone
        # from disk. Up to Django 5.0 every app's such migrations count; from
        # 5.1, only those of the app being pruned.
        ahead_apps = {app_label for app_label, _name in ahead}
        prunable = {
            key
            for key in set(loader.applied_migrations) - set(loader.disk_migrations)
            if django.VERSION < (5, 1) or key[0] in ahead_apps
        }
        unfinished = sorted(
            squash
            for squash, migration in loader.replacements.items()
            if prunable.intersection(migration.replaces)
        )
        stranded = _stranded_squashes(
            loader.applied_migrations,
            graph=loader.graph.nodes,
            replacements={key: squash.replaces for key, squash in loader.replacements.items()},
        )
    finally:
        connection.close()
        connection.settings_dict["NAME"] = original
    if unrecorded:
        tables = sorted(table for app_tables in unrecorded.values() for table in app_tables)
        raise UnusableBase(
            f"The base database {base!r} records no applied migrations for "
            f"{len(unrecorded)} app(s) whose tables it holds, {_examples(list(unrecorded))}, "
            f"with {len(tables)} table(s) among them, {_examples(tables)}. Nothing records "
            "which migrations made those tables, so migrating it would create them again and "
            "fail on the first. A base that migrate made records them in django_migrations; "
            "one restored from pg_dump --schema-only has that table but none of its rows, and "
            "one made while an app had no migrations has none for that app once it gains them. "
            "Restore the base with the rows of django_migrations as well as its schema, or "
            "recreate it with migrate, or pass base=None to build from empty."
        )
    if ahead:
        prune = " and ".join(
            f"manage.py migrate {app_label} --prune" for app_label in sorted(ahead_apps)
        )
        finish = (
            " A squashed migration still lists the migrations it replaces, "
            f"{_examples(_dotted(unfinished))}, among them one the base records as applied "
            "that is gone from disk, and --prune declines to run while one does: "
            "finish it first, by running manage.py migrate against the base so that it is "
            "recorded as applied and then removing its replaces attribute, which makes it an "
            "ordinary migration."
            if unfinished
            else ""
        )
        raise UnusableBase(
            f"The base database {base!r} is ahead of the migrations on disk: it records "
            f"{len(ahead)} applied migration(s) of installed apps that no migration on disk "
            f"is or replaces, {_examples(_dotted(ahead))}. Of the histories a base can "
            "record, this is the one migrating cannot bring to this checkout's schema: it moves "
            "only forward, so whatever those migrations did stays in the copy, and a branch "
            "migration that adds only an index would otherwise build silently and skew plan "
            "assertions. If "
            "the rows were left behind by squashed migrations whose files were deleted after "
            "the squash's replaces attribute was removed, run "
            f"{prune} against the base.{finish} If they come from another branch, migrate the "
            "base back from a checkout that has them, or recreate it."
        )
    if stranded:
        missing = sorted(key for keys in stranded.values() for key in keys)
        raise UnusableBase(
            f"The base database {base!r} has applied only part of {len(stranded)} squashed "
            f"migration(s), {_examples(_dotted(stranded))}, and {len(missing)} of the "
            "migrations they replace that it has yet to apply are not on disk, "
            f"{_examples(_dotted(missing))}. Django runs a squash only when all or none of "
            "what it replaces is applied, and otherwise runs the replaced migrations "
            "themselves, so with those files gone neither runs and the template would lack "
            "what they do. Migrate the base from a checkout that still has the replaced "
            "migrations, or recreate it."
        )


def _tables_of(connection: Any, app_labels: Iterable[str]) -> list[str]:
    """The tables ``run_syncdb`` would make for these apps that the connected database holds.

    Read the way ``migrate``'s ``run_syncdb`` phase reads them, because the
    drop list has to be exactly the set it then makes: a table dropped that it
    does not make again is lost, and one kept that it does make fails its
    create with ``already exists``. That is the models of the apps that have a
    models module, which ``run_syncdb`` requires before it looks at an app's
    models at all, that the router lets migrate on this alias, less what
    ``can_migrate`` refuses -- a proxy, an unmanaged
    or a swapped model -- and for each of those, the auto-created
    many-to-many tables of the relations it declares, which ``run_syncdb``
    makes only as part of making that model. Not the through models on their
    own: Django counts an auto-created through as managed when either end is,
    so the through of an unmanaged model's relation to a managed one reads as
    migratable and is still never made. A through model a relation names is a
    model of its own, and is judged as one. The names are compared as
    PostgreSQL stores them, by :func:`_stored_name`, because it shortens a long
    one where ``run_syncdb`` does not: there, a table the base holds under a
    shortened name is looked for under the full one, missed, and created a
    second time.

    Each condition is held by a test, because each filter is one arc to a
    branch gate:

    - a models module: ``test_an_app_with_no_models_module_is_carried``;
    - the router: ``test_a_table_the_router_keeps_off_the_alias_is_carried``;
    - ``can_migrate``: ``test_an_unmanaged_models_table_in_the_base_is_carried``,
      where dropping the table would lose it, since nothing makes it again;
    - the through tables, and only those of the models made:
      ``test_a_stale_through_table_is_rebuilt_with_the_model_that_declares_it``
      and ``test_the_through_table_of_an_unmanaged_model_is_carried``;
    - auto-created ones only:
      ``test_a_declared_through_model_is_judged_as_a_model_of_its_own``;
    - existing: ``test_an_empty_base_is_migrated_in_full``, and
      ``test_migrate_still_runs_over_the_copy``, whose base has no table of an
      app without migrations to drop;
    - existing under the name PostgreSQL stores:
      ``test_a_table_postgres_stores_under_a_shortened_name_is_rebuilt``, whose
      name straddles the limit with a two-byte character, so a cut by
      characters or one splitting the character fails it as well.
    """
    labels = set(app_labels)
    models = [
        model
        for app_config in apps.get_app_configs()
        if app_config.models_module is not None and app_config.label in labels
        for model in router.get_migratable_models(
            app_config, connection.alias, include_auto_created=False
        )
        if model._meta.can_migrate(connection)
    ]
    made = {model._meta.db_table for model in models}
    for model in models:
        for field in model._meta.local_many_to_many:
            # Optional on the relation's type, and set on every many-to-many
            # field by the time the app registry is ready, which it is here.
            through: Any = field.remote_field.through
            if through._meta.auto_created:
                made.add(through._meta.db_table)
    limit = connection.ops.max_name_length()
    stored = {_stored_name(table, limit) for table in made}
    return sorted(stored & set(connection.introspection.table_names()))


def _stored_name(name: str, limit: int) -> str:
    """``name`` as PostgreSQL stores it: its first ``limit`` bytes, cut back to a whole character.

    PostgreSQL shortens a longer identifier rather than refusing it, and Django
    shortens only a table name it makes up, never a ``db_table`` a model or a
    many-to-many field spells out, so the name a model gives and the name the
    catalogue lists can differ. Counted in UTF-8, which Django assumes every
    database it talks to uses. Decoding with ``errors="ignore"`` is the cut
    back: valid UTF-8 cut at a byte count can end only in part of one
    character, and that part is what it drops, as PostgreSQL does.
    """
    return name.encode()[:limit].decode(errors="ignore")


def _ahead_of_disk(
    applied: Iterable[tuple[str, str]],
    *,
    disk: Iterable[tuple[str, str]],
    replaced: Iterable[tuple[str, str]],
    installed: Iterable[str],
) -> list[tuple[str, str]]:
    """The applied migrations of installed apps that are neither on disk nor replaced.

    The replaced exclusion is what keeps an ordinary squash from refusing a good
    base: once the replaced files are deleted, their records stay behind in
    ``django_migrations``, legitimately, and the squash on disk names them in
    its ``replaces``. A squash applied only in part is excused here too, and
    answered by :func:`_stranded_squashes` instead, which is what can tell the
    part Django will finish from the part nothing will run. The installed one
    is what keeps a removed app from refusing it: its rows outlive it and
    describe no table the checkout's models use.

    Takes its inputs as arguments rather than reading a loader, for the reason
    :func:`_schema_digest` does: the exclusions need a squashed migration on
    disk and an app that is gone to reach through a real base, and as
    arithmetic they need nothing. Each condition is held by a test, because a
    branch gate sees the comprehension's filter as one arc and would stay green
    with any of them deleted:

    - not on disk: ``test_a_migration_on_disk_is_not_ahead_of_it``, and every
      test that builds from a base;
    - not replaced: ``test_unless_a_migration_on_disk_replaces_it``, and
      ``test_a_squash_whose_replaced_files_were_deleted_does_not_make_a_base_ahead``,
      which also holds that the replaced names are read off the disk at all;
    - installed: ``test_nor_one_recorded_for_an_app_that_is_not_installed``, and
      ``test_a_row_for_an_app_no_longer_installed_does_not_refuse_the_base``,
      which also holds that the labels are not simply every label the base
      records, while ``test_a_base_ahead_of_the_migrations_on_disk_is_refused``
      holds that the installed apps are among them.
    """
    on_disk = set(disk)
    excused = set(replaced)
    labels = set(installed)
    return sorted(
        key for key in applied if key not in on_disk and key not in excused and key[0] in labels
    )


def _stranded_squashes(
    applied: Iterable[tuple[str, str]],
    *,
    graph: Iterable[tuple[str, str]],
    replacements: Mapping[tuple[str, str], Iterable[tuple[str, str]]],
) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """The squashes applied in part, each with the replaced migrations it still needs and lacks.

    Django's loader runs a squash only when all or none of what it replaces is
    applied. In between, it sets the squash aside -- takes it out of the graph
    and keeps the replaced migrations -- so ``migrate`` finishes the job one
    replaced migration at a time, which ends where the squash would have,
    provided their files are there. With one deleted, nothing takes its place:
    the loader raises nothing, ``migrate`` reports nothing, and the copy
    silently lacks what the squash does under a key that names it, while a
    template from empty runs the squash and has it.

    Which squashes were set aside is read off the graph the loader built
    rather than worked out again here, by :func:`_set_aside`, because from
    Django 6.0 a squash can replace another squash and the loader judges "in
    part" over everything both of them come down to: an outer squash can be
    set aside while nothing it lists directly is applied. A squash set aside
    is stranded when one of the migrations it lists is unapplied, missing from
    the graph -- a file gone from disk -- and not a squash itself: one already
    applied needs no running, one in the graph will be run, and a squash is
    judged on its own entry rather than as a missing file of the one that
    replaces it.

    Takes its inputs as arguments for the reason :func:`_ahead_of_disk` does.
    Each condition is held by a test, because a branch gate sees each filter
    as one arc and would stay green with any of them deleted:

    - unapplied: ``test_but_one_already_applied_is_not``, which a real base
      reaches only through a dependency Django refuses first;
    - missing from the graph:
      ``test_a_squash_applied_in_part_is_finished_by_the_replaced_files_still_on_disk``;
    - not a squash itself: ``test_nor_one_that_is_itself_a_squash`` and, from
      Django 6.0,
      ``test_a_squash_of_a_squash_applied_in_part_is_finished_by_the_files_on_disk``;
    - set aside: the two conditions :func:`_set_aside` names;
    - and that a stranded squash refuses the base:
      ``test_a_squash_applied_in_part_with_its_replaced_files_deleted_is_refused``
      and, from Django 6.0,
      ``test_a_squash_of_a_squash_applied_in_part_with_a_replaced_file_deleted_is_refused``.
    """
    done = set(applied)
    in_graph = set(graph)
    replaced = {squash: list(keys) for squash, keys in replacements.items()}
    replacers = {
        squash: [outer for outer, keys in replaced.items() if squash in keys] for squash in replaced
    }
    stranded: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for squash, keys in sorted(replaced.items()):
        missing = [
            key for key in keys if key not in done and key not in in_graph and key not in replaced
        ]
        if missing and _set_aside(squash, graph=in_graph, replacers=replacers):
            stranded[squash] = missing
    return stranded


def _set_aside(
    squash: tuple[str, str],
    *,
    graph: set[tuple[str, str]],
    replacers: Mapping[tuple[str, str], list[tuple[str, str]]],
) -> bool:
    """Whether the loader set this squash aside, rather than using it or replacing it.

    A squash is missing from the loader's graph for one of two reasons. Either
    it was set aside, applied in part, or it was used -- all or none of it
    applied -- and then replaced in turn by an outer squash that was used too,
    which takes what it replaces out of the graph as any squash does. A squash
    used is in the graph unless a used squash replaces it, so the outermost
    used one always is; and a squash set aside has only squashes set aside
    above it, since whatever is applied under it is under them as well. So a
    squash was set aside when it is missing from the graph and so is every
    squash that replaces it, directly or further out.

    Both halves are held by a test, because they are one arc to a branch gate:

    - missing from the graph: ``test_a_base_behind_a_squash_is_migrated_through_it``,
      whose squash is used and whose replaced migrations it took out of the graph;
    - and so is every squash replacing it:
      ``test_nor_one_under_a_squash_the_loader_replaced_rather_than_set_aside`` and,
      from Django 6.0, ``test_a_base_behind_a_squash_of_a_squash_is_migrated_through_it``.
    """
    return squash not in graph and all(
        _set_aside(outer, graph=graph, replacers=replacers) for outer in replacers[squash]
    )


def _dotted(migrations: Iterable[tuple[str, str]]) -> list[str]:
    """Migration keys as ``app_label.name``, the way Django prints them."""
    return [f"{app_label}.{name}" for app_label, name in migrations]


def _examples(names: list[str]) -> str:
    """A few names, and how many more there are."""
    named = ", ".join(names[:_EXAMPLES])
    rest = len(names) - _EXAMPLES
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
