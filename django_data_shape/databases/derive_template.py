from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from django.db import DEFAULT_DB_ALIAS, connections
from django.db.transaction import TransactionManagementError

from django_data_shape.backends.require_clone_strategy import require_clone_strategy
from django_data_shape.backends.require_postgres import require_postgres
from django_data_shape.databases.clone_database import clone_database
from django_data_shape.databases.step_failed import StepFailed
from django_data_shape.utils import (
    FINISHED_TEMPLATE,
    PARTIAL_SUFFIX,
    TEMPLATE_KEY_BYTES,
    TEMPLATE_PREFIX,
    absorb,
    pointed_at,
    template_lock_id,
)

# Its own format tag, so a derived name can never be the name template_database
# would give some shape: the two digests absorb different things, and the tag is
# what keeps them in different spaces.
_FORMAT = "django-data-shape derived template 1"


def derive_template(
    template: str,
    step: Callable[[str], object],
    *,
    key: str,
    using: str = DEFAULT_DB_ALIAS,
    strategy: str | None = "file_copy",
) -> str:
    """Make sure a template holding ``template`` with ``step`` run over it exists; return its name.

    For the world an application finishes itself. A shape describes what can be
    declared; some columns are the application's own arithmetic -- running
    totals a replay through its services fills in -- and no declaration can
    describe them. Run that code over a fresh test database every session and
    the cache pays for the cheap half only: measured on a consumer's world, the
    template took 2.5 s to build and the replay over it 93 s. This puts the
    replay inside the cache. The step runs once per machine, over a copy of
    ``template``, and the result is a template like any other, cloned with
    :func:`~django_data_shape.databases.clone_database.clone_database` in a
    fraction of a second::

        template = template_database(shape)
        finished = derive_template(template, replay_draws, key="replay-draws-3")
        clone_database(finished, "test_myapp", replace=True)

    **What the step is handed.** ``step(using)`` is called with the connection
    alias pointed at the copy, under its working name, the way the build itself
    reaches a template: the ORM, ``connections[using]`` and anything else going
    through that alias write into the copy. Its return value is ignored. It runs
    outside any transaction this function opens, so what it commits is what the
    template holds; a step that leaves another connection attached to the copy
    stops the copy being renamed, as anything attached to a database does.

    **``key`` is required, and it is the step's identity.** A callable cannot be
    hashed, for the reason a shape holding a
    :class:`~django_data_shape.derivations.derived.Derived` is refused: two
    closures agree while the data differs, which is the one direction a cache key
    must never be wrong. So the caller names the step, and **changes the key when
    the step changes**. The template's name is a digest of ``template``'s name
    and ``key``, and ``template``'s name is itself a digest of the shape, the
    schema, the settings, the base and this package's version, so a change to any
    of those makes a new template here as well, without the key having to say so.

    **The cache trusts the step to be a function of the world it is given.** A
    step reading the clock, drawing unseeded randomness or reading anything
    outside the database produces whatever its first run produced, under a key
    that claims more. That is stated rather than detected, as the rows a base
    holds are for ``template_database``.

    **The whole database is analyzed after the step**, not the tables the shape
    declared: the step may have written to tables the shape never named, and a
    table with no statistics is worse than a small one. Anything else that
    writes to a world after it is built leaves its statistics stale, whether or
    not it goes through this function.

    **Steps chain**, because a derived template is a template: derive from one
    to run a second step over the first, and changing the second step's key
    re-runs only the second step. Changing ``key`` never rebuilds what is under
    it, which is why this derives from a template rather than taking the step as
    an argument to ``template_database``.

    **A step that raises leaves nothing behind.** The copy is dropped, so the
    next call starts again from ``template``, and the exception is raised again
    as :class:`~django_data_shape.databases.step_failed.StepFailed`, naming the
    key, with the step's own exception as its cause. A ``KeyboardInterrupt`` or
    any other ``BaseException`` drops the copy and goes on unwrapped.

    **What it refuses**, each before anything is created:

    - **A ``template`` this package did not finish.** Its name has to be one
      ``template_database`` or this function returned, a prefix and a digest,
      because the derived name is keyed on it: a database of the caller's own
      keeps its name while its rows change, and a template derived from it would
      be served stale. A partial is refused for the same reason, and so is a
      name holding a double quote, which no generated name does. ``ValueError``,
      because what is wrong is an argument.
    - **A ``template`` that does not exist**, when the derived one is not there
      either. ``ValueError`` again, naming it. On a hit the parent is not
      needed and not looked for, so a parent dropped after deriving from it
      costs nothing until the derived template goes too.
    - **Anything but PostgreSQL**, and **being called inside a transaction**,
      for the reasons ``template_database`` gives: there is no ``CREATE
      DATABASE ... TEMPLATE`` elsewhere, and pointing the connection at the copy
      closes it, which leaves an atomic block unusable.
    - **A ``strategy`` the server cannot take.** It means what it means for
      :func:`~django_data_shape.databases.clone_database.clone_database`, which
      makes the copy: ``file_copy`` by default, because a world worth deriving
      is a world worth copying cheaply, which a PostgreSQL older than 15 refuses
      unless the caller passes ``strategy=None``. It is not part of the key,
      because it decides what the copy costs and not what it holds.

    Parallel workers asking for the same derived template wait on one advisory
    lock, taken on its name, while one of them runs the step. The finished
    template refuses connections, as every template this package makes does;
    nothing removes it, and :func:`~django_data_shape.databases.drop_database.drop_database`
    is how a stale one goes.
    """
    connection: Any = connections[using]
    require_postgres(connection, "Deriving a template database")
    if connection.in_atomic_block:
        raise TransactionManagementError(
            "Deriving a template database points the connection at a copy and closes it, which "
            "cannot be done inside an atomic block -- the connection would be unusable for the "
            "rest of it. Call this from test session setup, before anything has opened a "
            "transaction."
        )
    require_clone_strategy(connection, strategy)
    if not FINISHED_TEMPLATE.fullmatch(template):
        raise ValueError(
            f"{template!r} is not a template this package finished, so nothing can be derived "
            "from it. A derived template is named after its parent's name and the key, which "
            "keeps up with the data only when the parent's name is itself a digest of "
            "everything under it, as the names template_database and derive_template return "
            "are: a database of your own keeps its name while its rows change, and a template "
            "derived from it would be served stale. Pass a name one of those two returned."
        )
    name = f"{TEMPLATE_PREFIX}{_key(template, key)}"
    quote = connection.ops.quote_name
    with connection._nodb_cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [template_lock_id(name)])
        try:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name])
            if cursor.fetchone() is None:
                _create(connection, cursor, template, step, key, name, using, quote, strategy)
        finally:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [template_lock_id(name)])
    return name


def _create(
    connection: Any,
    cursor: Any,
    template: str,
    step: Callable[[str], object],
    key: str,
    name: str,
    using: str,
    quote: Any,
    strategy: str | None,
) -> None:
    """Copy the parent, run the step and ``ANALYZE`` over it, then rename it into place."""
    partial = f"{name}{PARTIAL_SUFFIX}"
    cursor.execute(f"DROP DATABASE IF EXISTS {quote(partial)}")
    cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [template])
    if cursor.fetchone() is None:
        raise ValueError(
            f"The template {template!r} does not exist, so nothing can be derived from it. Make "
            "it again with template_database, or with derive_template if it was derived, and "
            "derive from the name that returns."
        )
    clone_database(template, partial, using=using, strategy=strategy)
    try:
        with pointed_at(connection, partial):
            step(using)
            with connection.cursor() as analyzing:
                analyzing.execute("ANALYZE")
    except Exception as failed:
        cursor.execute(f"DROP DATABASE IF EXISTS {quote(partial)}")
        raise StepFailed(
            f"The step deriving a template from {template!r} under key {key!r} raised "
            f"{type(failed).__name__}: {failed}. Nothing was kept: the copy it ran in was "
            "dropped, so the next call runs the step again from the same template."
        ) from failed
    except BaseException:
        cursor.execute(f"DROP DATABASE IF EXISTS {quote(partial)}")
        raise
    cursor.execute(f"ALTER DATABASE {quote(partial)} RENAME TO {quote(name)}")
    cursor.execute(f"ALTER DATABASE {quote(name)} WITH ALLOW_CONNECTIONS false")


def _key(template: str, key: str) -> str:
    """The parent's name and the caller's key, digested to the length a template name takes."""
    hasher = hashlib.blake2b(digest_size=TEMPLATE_KEY_BYTES)
    absorb(hasher, _FORMAT)
    absorb(hasher, template)
    absorb(hasher, key)
    return hasher.hexdigest()
