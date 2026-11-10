from __future__ import annotations

from typing import Any

from django.db import DEFAULT_DB_ALIAS, connections

from django_data_shape.backends.require_postgres import require_postgres
from django_data_shape.databases.require_quotable_name import require_quotable_name


def drop_database(name: str, *, using: str = DEFAULT_DB_ALIAS) -> bool:
    """Drop ``name`` if it is there, and return whether it was."""
    connection: Any = connections[using]
    require_postgres(connection, "Dropping a database")
    require_quotable_name(name, "database")
    quote = connection.ops.quote_name
    with connection._nodb_cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name])
        existed = cursor.fetchone() is not None
        cursor.execute(f"DROP DATABASE IF EXISTS {quote(name)}")
    return existed
