"""The other backend gate, for the one clause that decides what a clone costs."""

from __future__ import annotations

from typing import Any

from django_data_shape.unsupported_backend import UnsupportedBackend

# The only two PostgreSQL defines, and an allowlist rather than a passthrough:
# STRATEGY takes no bound parameter, so whatever is given is interpolated into
# the statement.
STRATEGIES = ("file_copy", "wal_log")

# ``CREATE DATABASE ... STRATEGY`` arrived in PostgreSQL 15. Below that the
# clause is a syntax error rather than a slower plan, so it is refused by name.
_STRATEGY_SINCE = 150_000


def require_clone_strategy(connection: Any, strategy: str | None) -> None:
    """Refuse a strategy this server cannot take, and one it has never heard of.

    Its own function for the reason
    :func:`~django_data_shape.require_postgres.require_postgres` is: it reads
    ``pg_version`` off the connection and nothing else, so the refusal is
    covered by passing a version rather than by installing a PostgreSQL 14 to
    run the suite against. A degradation path reachable only on the
    configuration it degrades for is a path this package's coverage gate cannot
    see, and that gate is on PostgreSQL precisely because that is where the work
    happens.

    ``None`` is always allowed: it means no clause at all, which is what every
    version does when nobody asks for a strategy.
    """
    if strategy is None:
        return
    if strategy not in STRATEGIES:
        # A plain ValueError rather than one of this package's own types.
        # Nothing is wrong with a shape, a declaration or the backend; an
        # argument is wrong, which is what ValueError is for.
        raise ValueError(
            f"A clone strategy must be one of {', '.join(STRATEGIES)}, or None to take the "
            f"server's default; got {strategy!r}."
        )
    if connection.pg_version < _STRATEGY_SINCE:
        raise UnsupportedBackend(
            f"CREATE DATABASE ... STRATEGY needs PostgreSQL 15; connection '{connection.alias}' "
            f"reports {connection.pg_version}. Pass strategy=None to clone with the server's own "
            "default, which is what every version does when the clause is absent. It is slower -- "
            "five to eight times, measured -- and it is the only thing that changes."
        )
