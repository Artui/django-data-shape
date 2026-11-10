from __future__ import annotations

from typing import Any

from django.db import DEFAULT_DB_ALIAS, connections

from django_data_shape.backends.require_clone_strategy import require_clone_strategy
from django_data_shape.backends.require_postgres import require_postgres
from django_data_shape.databases.require_quotable_name import require_quotable_name


def clone_database(
    template: str,
    target: str,
    *,
    using: str = DEFAULT_DB_ALIAS,
    strategy: str | None = "file_copy",
    replace: bool = False,
) -> None:
    """Copy ``template`` into ``target`` with ``CREATE DATABASE``, the cheap half of the cache."""
    connection: Any = connections[using]
    require_postgres(connection, "Cloning a database")
    require_quotable_name(template, "template database")
    require_quotable_name(target, "target database")
    require_clone_strategy(connection, strategy)

    quote = connection.ops.quote_name
    statement = f"CREATE DATABASE {quote(target)} TEMPLATE {quote(template)}"
    if strategy is not None:
        statement = f"{statement} STRATEGY = {strategy}"
    with connection._nodb_cursor() as cursor:
        if replace:
            cursor.execute(f"DROP DATABASE IF EXISTS {quote(target)}")
        cursor.execute(statement)
