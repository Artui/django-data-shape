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
    """Return a template holding ``template`` with ``step`` run over it, built once per key."""
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
