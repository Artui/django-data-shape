from __future__ import annotations

import hashlib
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
from django_data_shape.utils import (
    FINISHED_TEMPLATE,
    PARTIAL_SUFFIX,
    TEMPLATE_KEY_BYTES,
    TEMPLATE_PREFIX,
    absorb,
    pointed_at,
    template_lock_id,
)
from django_data_shape.version import __version__

_FORMAT = "django-data-shape template 1"

# How many migrations or tables a refused base's message names. Enough to say
# which app and roughly how far, without pasting a whole history into a
# traceback when a base is dozens of migrations out.
_EXAMPLES = 3

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
    connection: Any = connections[using]
    require_postgres(connection, "Caching a shape as a template database")
    if connection.in_atomic_block:
        raise TransactionManagementError(
            "Caching a shape as a template database points the connection at another database "
            "and closes it, which cannot be done inside an atomic block -- the connection would "
            "be unusable for the rest of it. Call this from test session setup, before anything "
            "has opened a transaction."
        )

    context = _context() if base is None else (*_context(), *_base_context(connection, base))
    name = f"{TEMPLATE_PREFIX}{_key(shape, context)}"
    quote = connection.ops.quote_name
    with connection._nodb_cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [template_lock_id(name)])
        try:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name])
            if cursor.fetchone() is None:
                _create(shape, connection, cursor, name, using, quote, base)
        finally:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [template_lock_id(name)])
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
    """Build under a working name and rename it into place, which is the commit."""
    partial = f"{name}{PARTIAL_SUFFIX}"
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
    """Migrate the schema into ``database``, and build the shape there."""
    with pointed_at(connection, database):
        if base is not None:
            _drop_unmigrated_tables(connection, base)
        call_command("migrate", database=using, run_syncdb=True, interactive=False, verbosity=0)
        build(shape, using=using)


def _drop_unmigrated_tables(connection: Any, base: str) -> None:
    """Drop the copy's tables for unmigrated apps, so ``run_syncdb`` makes them again."""
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
    """Refuse a base no migration can repair, and return its name and database oid."""
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
    if _is_generated_template(base):
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
    """Refuse a base whose applied history migrating cannot carry to the disk's schema."""
    with pointed_at(connection, base):
        loader = MigrationLoader(connection)
        recorded = {app_label for app_label, _name in loader.applied_migrations}
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
    """The tables ``run_syncdb`` would make for these apps that the database holds."""
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
            through: Any = field.remote_field.through
            if through._meta.auto_created:
                made.add(through._meta.db_table)
    limit = connection.ops.max_name_length()
    stored = {_stored_name(table, limit) for table in made}
    return sorted(stored & set(connection.introspection.table_names()))


def _stored_name(name: str, limit: int) -> str:
    """``name`` as PostgreSQL stores it, cut to ``limit`` bytes on a whole character."""
    return name.encode()[:limit].decode(errors="ignore")


def _ahead_of_disk(
    applied: Iterable[tuple[str, str]],
    *,
    disk: Iterable[tuple[str, str]],
    replaced: Iterable[tuple[str, str]],
    installed: Iterable[str],
) -> list[tuple[str, str]]:
    """Applied migrations of installed apps that are neither on disk nor replaced."""
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
    """Squashes applied in part, each with the replaced migrations it needs and lacks."""
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
    """Whether the loader set this squash aside, applied in part, rather than used it."""
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
    """Digest the declaration and the context that decides what gets built."""
    hasher = hashlib.blake2b(digest_size=TEMPLATE_KEY_BYTES)
    absorb(hasher, _FORMAT)
    absorb(hasher, shape_digest(shape))
    for part in context:
        absorb(hasher, part)
    return hasher.hexdigest()


def _context() -> tuple[str, ...]:
    """The package version, the schema and the datetime settings a build depends on."""
    return (
        __version__,
        _schema_digest(
            tuple(apps.get_models()), tuple(sorted(MigrationLoader(None).disk_migrations))
        ),
        str(settings.USE_TZ),
        str(settings.TIME_ZONE),
    )


def _schema_digest(models: tuple[Any, ...], migrations: tuple[tuple[str, str], ...]) -> str:
    """Digest the migrations on disk and the current model fields."""
    hasher = hashlib.blake2b(digest_size=TEMPLATE_KEY_BYTES)
    for app_label, migration in sorted(migrations):
        absorb(hasher, f"{app_label}.{migration}")
    for model in sorted(models, key=lambda model: str(model._meta.label)):
        absorb(hasher, f"{model._meta.label}.{model._meta.db_table}")
        for field in model._meta.concrete_fields:
            absorb(hasher, f"{field.name}|{field.column}|{field.get_internal_type()}|{field.null}")
    return hasher.hexdigest()


def _is_generated_template(name: str) -> bool:
    """Whether ``name`` is one this package generates for a template, finished or partial."""
    return FINISHED_TEMPLATE.fullmatch(name.removesuffix(PARTIAL_SUFFIX)) is not None
