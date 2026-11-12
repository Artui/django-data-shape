from __future__ import annotations

from typing import Any

from django.db import DEFAULT_DB_ALIAS, connections

from django_data_shape.backends.require_postgres import require_postgres
from django_data_shape.databases.require_quotable_name import require_quotable_name


def drop_database(name: str, *, using: str = DEFAULT_DB_ALIAS) -> bool:
    """Drop ``name`` if it is there, and say whether it was.

    Public for two reasons, and the second is the one that makes it worth a
    module. A test-database setup that clones per session has to remove the
    clone again, and writing that as raw SQL in a ``conftest.py`` means writing
    the autocommit rule and the quoting by hand.

    And **a template database is never removed automatically**. It is a cache on
    a machine, keyed by content, so a template that no shape asks for any more is
    a template nothing will ever open again -- and deleting it on a guess would
    mean this package dropping a database because it did not recognise a name.
    So they accumulate, and this is how a stale one goes. They are named
    ``data_shape_`` followed by a digest; ``psql -c "\\l data_shape_*"`` lists
    them.

    ``ALLOW_CONNECTIONS`` being off on a finished template does not get in the
    way: ``DROP DATABASE`` does not connect to what it drops. What does get in
    the way is somebody else's open session, which PostgreSQL reports by name.

    **A name holding a double quote raises ``ValueError``** before anything is
    asked of the server. The name is looked up as a value and dropped as an
    identifier, and Django's quoting carries a double quote into the second
    unescaped -- a name already wrapped in them is passed through as it is -- so
    ``'"x"'`` would be reported missing while ``x`` was dropped. No database
    this package makes is named that way.
    """
    connection: Any = connections[using]
    require_postgres(connection, "Dropping a database")
    require_quotable_name(name, "database")
    quote = connection.ops.quote_name
    with connection._nodb_cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name])
        existed = cursor.fetchone() is not None
        cursor.execute(f"DROP DATABASE IF EXISTS {quote(name)}")
    return existed
