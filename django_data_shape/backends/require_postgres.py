from __future__ import annotations

from typing import Any

from django_data_shape.backends.unsupported_backend import UnsupportedBackend


def require_postgres(connection: Any, operation: str, *, statistics: bool = True) -> None:
    """Refuse anything but PostgreSQL, and psycopg 2, naming what was refused."""
    driver = getattr(connection.Database, "__name__", "")
    if connection.vendor == "postgresql" and driver != "psycopg":
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
