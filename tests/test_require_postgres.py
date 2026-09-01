"""The backend gate, covered by passing a vendor rather than a database."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from django_data_shape import UnsupportedBackend
from django_data_shape.require_postgres import require_postgres


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


def test_postgresql_passes() -> None:
    require_postgres(_Connection(vendor="postgresql"), "Building a shape")


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
