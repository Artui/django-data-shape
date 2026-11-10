from __future__ import annotations

from typing import Any

import pytest

from django_data_shape.backends.require_postgres import require_postgres
from django_data_shape.backends.unsupported_backend import UnsupportedBackend


def skip_unless_postgres(connection: Any, operation: str) -> None:
    """Skip the current test, with a stated reason, where ``operation`` cannot mean anything."""
    try:
        require_postgres(connection, operation)
    except UnsupportedBackend as unsupported:
        pytest.skip(str(unsupported))
