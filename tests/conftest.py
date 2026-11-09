"""Suite-wide fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from django_data_shape.databases import template_database as template_database_module


@pytest.fixture(autouse=True, scope="session")
def templates_of_this_run_alone() -> Iterator[None]:
    """Give every template this run builds a name no other run on the server can build.

    A template's name is a digest of the declaration and the world around it, and
    templates are server-wide, so two suites on one PostgreSQL server that
    declare the same shape ask for the same database -- whatever their
    ``PGDATABASE``. One then reads a template the other has half made, or has
    its template dropped by the other's teardown, and the two fail each other in
    different places on different runs. Keying the context on the database the
    run was pointed at keeps them apart, and costs nothing a single run could
    notice: the same ``PGDATABASE`` is the same run, which shares its test
    database already and could not share it with another.

    The package's own ``_context`` is wrapped rather than replaced, so the key
    still moves with everything it moves with in production.
    """
    salt = os.environ.get("PGDATABASE", "django_data_shape")
    real = template_database_module._context

    def salted() -> tuple[str, ...]:
        return (*real(), salt)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(template_database_module, "_context", salted)
        yield
