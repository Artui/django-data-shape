"""Making a database out of one that is already built."""

from __future__ import annotations

from typing import Any

from django.db import DEFAULT_DB_ALIAS, connections

from django_data_shape.require_clone_strategy import require_clone_strategy
from django_data_shape.require_postgres import require_postgres


def clone_database(
    template: str,
    target: str,
    *,
    using: str = DEFAULT_DB_ALIAS,
    strategy: str | None = "file_copy",
    replace: bool = False,
) -> None:
    """``CREATE DATABASE target TEMPLATE template``, which is the cheap half of this package.

    The operation the whole template cache exists to reach. Building a shaped
    database is expensive and copying one is not: measured on a 212 MB database,
    the build was about nineteen seconds and this is **174 ms** with
    ``STRATEGY = file_copy``, against 0.85 to 1.45 s on PostgreSQL's default
    ``wal_log``. The statistics come with it -- ``pg_statistic`` rows and the
    per-column targets in ``pg_attribute`` are ordinary catalogue contents, so
    the clone is planner-ready without a second ``ANALYZE``.

    ``strategy`` defaults to ``file_copy`` because that is the whole point.
    ``wal_log`` writes every copied page through the write-ahead log, which is
    what makes a clone crash-safe and point-in-time recoverable -- properties a
    test database created fresh each session has no use for, paid for at five to
    eight times the cost. ``None`` leaves the clause off and takes the server's
    own default, which is the setting for a PostgreSQL older than 15.

    **Nothing may be connected to the template while this runs.** PostgreSQL
    refuses to copy a database that has other backends attached, with "source
    database is being accessed by other users", and the usual cause is the
    process doing the cloning: a Django connection left open from building it,
    or a ``psql`` somebody forgot. That is why
    :func:`~django_data_shape.template_database.template_database` closes its
    connection and turns connections off on the finished template rather than
    trusting nobody will open one. Concurrent *clones* of one template are fine
    and are what a parallel test run does -- PostgreSQL serialises them.

    ``replace=True`` drops ``target`` first. It is not the default because this
    function destroys a database, and a default that destroys is a default
    somebody meets by accident; a test-database setup that reruns wants it, and
    says so.

    Both names are quoted by the connection rather than interpolated raw, and
    the strategy is chosen from a fixed set, because none of the three can be a
    bound parameter: ``CREATE DATABASE`` is a utility statement whose grammar
    has no placeholders.
    """
    # Typed loosely for the reason the loader's connections are: ``pg_version``
    # and ``_nodb_cursor`` are the PostgreSQL wrapper's, not the base class's,
    # so naming the real type would mean asserting the checker out of the way
    # on every line that uses them.
    connection: Any = connections[using]
    require_postgres(connection, "Cloning a database")
    require_clone_strategy(connection, strategy)

    quote = connection.ops.quote_name
    statement = f"CREATE DATABASE {quote(target)} TEMPLATE {quote(template)}"
    if strategy is not None:
        statement = f"{statement} STRATEGY = {strategy}"
    # ``_nodb_cursor`` is Django's own way to reach a connection that is not
    # attached to the database being altered, and it is what Django's test
    # runner uses to create and drop test databases. It is also autocommit,
    # which CREATE DATABASE requires: it cannot run inside a transaction block.
    with connection._nodb_cursor() as cursor:
        if replace:
            cursor.execute(f"DROP DATABASE IF EXISTS {quote(target)}")
        cursor.execute(statement)
