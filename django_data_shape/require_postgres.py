"""The backend gate, in one place and testable without a database."""

from __future__ import annotations

from typing import Any

from django_data_shape.unsupported_backend import UnsupportedBackend


def require_postgres(connection: Any, operation: str) -> None:
    """Refuse anything but PostgreSQL, naming what was refused and why.

    Takes the connection and reads ``vendor`` off it rather than importing a
    backend or opening a cursor, which is what lets every refusal path in this
    package be covered by passing an object with a vendor. A degradation path
    reachable only by running the whole suite on the backend it refuses is a
    path the coverage gate cannot see, and this package gates coverage on
    Postgres precisely because that is where its real work happens.
    """
    if connection.vendor != "postgresql":
        raise UnsupportedBackend(
            f"{operation} needs PostgreSQL; connection '{connection.alias}' is "
            f"{connection.vendor}. Generation and cardinality are backend-neutral, but "
            "COPY loading and planner statistics are not, and a shaped database whose plans "
            "mean nothing is worse than no shaped database at all."
        )
