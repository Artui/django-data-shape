from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from django_data_shape import UnsupportedBackend
from django_data_shape.backends.require_postgres import require_postgres

# SimpleNamespace, not a class: ``type.__name__`` is a data descriptor on the
# metaclass, so a class-body ``__name__`` never wins and a stub class would keep
# reporting its own name. Django sets ``Database`` to a module anyway, which is
# what this stands in for.
_PSYCOPG3 = SimpleNamespace(__name__="psycopg")
_PSYCOPG2 = SimpleNamespace(__name__="psycopg2")


@dataclass
class _Connection:
    """Everything the gate reads, and nothing else."""

    vendor: str
    alias: str = "default"
    Database: Any = field(default_factory=lambda: _PSYCOPG3)


def test_postgresql_on_psycopg3_passes() -> None:
    require_postgres(_Connection(vendor="postgresql"), "Building a shape")


def test_postgresql_on_psycopg2_is_refused() -> None:
    # Django 6.1 still ships the psycopg 2 fallback, so this is a live configuration.
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
    # Any one missing leaves the reader to go and find it.
    assert "Building a shape" in message
    assert "reporting" in message
    assert vendor in message


@pytest.mark.parametrize("vendor", ["sqlite", "mysql"])
def test_another_backend_is_allowed_when_no_statistics_are_required(vendor: str) -> None:
    # A query count means the same on any backend; only a request for a plan is refused.
    require_postgres(_Connection(vendor=vendor), "Building a shape", statistics=False)


def test_but_psycopg2_is_refused_even_when_no_statistics_are_required() -> None:
    # The driver check is not part of that bargain.
    with pytest.raises(UnsupportedBackend, match="needs psycopg 3"):
        require_postgres(
            _Connection(vendor="postgresql", Database=_PSYCOPG2),
            "Building a shape",
            statistics=False,
        )
