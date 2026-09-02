"""The backend gate, in one place and testable without a database."""

from __future__ import annotations

from typing import Any

from django_data_shape.unsupported_backend import UnsupportedBackend


def require_postgres(connection: Any, operation: str, *, statistics: bool = True) -> None:
    """Refuse anything but PostgreSQL, naming what was refused and why.

    Takes the connection and reads ``vendor`` off it rather than importing a
    backend or opening a cursor, which is what lets every refusal path in this
    package be covered by passing an object with a vendor. A degradation path
    reachable only by running the whole suite on the backend it refuses is a
    path the coverage gate cannot see, and this package gates coverage on
    Postgres precisely because that is where its real work happens.

    ``statistics=False`` says the caller is asking for rows and cardinality
    rather than for a database the planner can reason about, so any vendor is
    allowed. **The driver check is not part of that bargain and still runs**: a
    psycopg 2 connection to PostgreSQL takes the ``COPY`` path whatever the
    caller asked for, and fails inside this package with a message about a
    missing attribute rather than about a missing driver.
    """
    # The driver is read off the connection, exactly like ``vendor`` above, and
    # for the same reason: a refusal that could only be covered by installing
    # the driver it refuses is a refusal the coverage gate cannot see. Django
    # sets ``Database`` to the driver module -- ``psycopg`` on 3, ``psycopg2``
    # on 2 -- so a stub can supply it and this branch is testable everywhere.
    driver = getattr(connection.Database, "__name__", "")
    if connection.vendor == "postgresql" and driver != "psycopg":
        # Django 6.1 still ships the psycopg 2 fallback, so this is a live
        # configuration rather than a legacy one. Without this check the vendor
        # gate passes and the load fails deep inside the package with
        # "'psycopg2.extensions.cursor' object has no attribute 'copy'" -- a
        # traceback pointing here, which a user would reasonably file as a bug
        # in this package rather than as a missing driver.
        raise UnsupportedBackend(
            f"{operation} needs psycopg 3; connection '{connection.alias}' is using "
            f"{driver or 'an unknown driver'}. "
            "Rows are streamed straight into COPY FROM STDIN, which psycopg 2 cannot do without "
            "materialising them first -- the cost this package exists to avoid. Install the "
            "'postgres' extra: pip install django-data-shape[postgres]."
        )
    if statistics and connection.vendor != "postgresql":
        raise UnsupportedBackend(
            f"{operation} needs PostgreSQL; connection '{connection.alias}' is "
            f"{connection.vendor}. Generation and cardinality are backend-neutral, but "
            "COPY loading and planner statistics are not, and a shaped database whose plans "
            "mean nothing is worse than no shaped database at all."
        )
