"""The backend gate, covered by passing a vendor rather than a database."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from django_data_shape import UnsupportedBackend
from django_data_shape.require_postgres import require_postgres

# SimpleNamespace, not a class: ``type.__name__`` is a data descriptor on the
# metaclass, so a class-body ``__name__`` never wins and a stub class would keep
# reporting its own name. Django sets ``Database`` to a module anyway, which is
# what this stands in for.
_PSYCOPG3 = SimpleNamespace(__name__="psycopg")
_PSYCOPG2 = SimpleNamespace(__name__="psycopg2")


@dataclass
class _Connection:
    """Everything the gate reads, and nothing else.

    A stub rather than a second database because the gate's whole design is that
    it branches on ``vendor``. If covering this needed a real SQLite connection,
    the refusal path would be unreachable from the Postgres job that carries the
    coverage gate -- which is the reason the gate reads a vendor at all.
    """

    vendor: str
    alias: str = "default"
    Database: Any = field(default_factory=lambda: _PSYCOPG3)


def test_postgresql_on_psycopg3_passes() -> None:
    require_postgres(_Connection(vendor="postgresql"), "Building a shape")


def test_postgresql_on_psycopg2_is_refused() -> None:
    # Django 6.1 still ships the psycopg 2 fallback, so this is a live
    # configuration. Without the check the vendor gate passes and the load dies
    # inside the package on "'psycopg2.extensions.cursor' object has no
    # attribute 'copy'" -- a traceback a user would reasonably file here.
    with pytest.raises(UnsupportedBackend, match="needs psycopg 3") as raised:
        require_postgres(
            _Connection(vendor="postgresql", alias="legacy", Database=_PSYCOPG2),
            "Building a shape",
        )

    assert "psycopg2" in str(raised.value)
    assert "django-data-shape[postgres]" in str(raised.value)


@pytest.mark.parametrize("vendor", ["sqlite", "mysql", "oracle"])
def test_every_other_backend_is_refused(vendor: str) -> None:
    with pytest.raises(UnsupportedBackend) as raised:
        require_postgres(_Connection(vendor=vendor, alias="reporting"), "Building a shape")

    message = str(raised.value)
    # The message has to name all three: what was refused, which connection, and
    # what that connection actually is. Any one missing leaves the reader to go
    # and find it.
    assert "Building a shape" in message
    assert "reporting" in message
    assert vendor in message


@pytest.mark.parametrize("vendor", ["sqlite", "mysql"])
def test_another_backend_is_allowed_when_no_statistics_are_required(vendor: str) -> None:
    # The relaxed gate, which is what lets a growth world exist off Postgres: a
    # query count is an ORM property and means the same anywhere, so the vendor
    # is only refused when the caller asked for a plan to be meaningful.
    require_postgres(_Connection(vendor=vendor), "Building a shape", statistics=False)


def test_but_psycopg2_is_refused_even_when_no_statistics_are_required() -> None:
    # The driver check is not part of that bargain. A psycopg 2 connection to
    # Postgres still takes the COPY path -- the vendor decides the route, not the
    # caller -- so relaxing the vendor gate must not relax this one, or the load
    # dies on a missing attribute instead of a missing driver.
    with pytest.raises(UnsupportedBackend, match="needs psycopg 3"):
        require_postgres(
            _Connection(vendor="postgresql", Database=_PSYCOPG2),
            "Building a shape",
            statistics=False,
        )
