from __future__ import annotations


def require_quotable_name(name: str, role: str, *, refusal: type[Exception] = ValueError) -> None:
    """Refuse a database name holding a double quote, before any statement uses it."""
    if '"' in name:
        raise refusal(
            f"The {role} name {name!r} contains a double quote. Django quotes a database "
            "name by wrapping it in double quotes, passes one that is already wrapped through "
            "unchanged and escapes nothing inside it, so a statement could name a different "
            "database from the one this name means where it is passed as a value -- a lookup "
            "in pg_database, or a connection's NAME. Use a name without one; a database "
            "already named so can be renamed in psql, writing each double quote inside the "
            "name twice."
        )
