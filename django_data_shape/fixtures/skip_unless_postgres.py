"""Degrading honestly, in the one form pytest understands."""

from __future__ import annotations

from typing import Any

import pytest

from django_data_shape.require_postgres import require_postgres
from django_data_shape.unsupported_backend import UnsupportedBackend


def skip_unless_postgres(connection: Any, operation: str) -> None:
    """Skip the current test, with a stated reason, where ``operation`` cannot mean anything.

    The pytest twin of
    :func:`~django_data_shape.require_postgres.require_postgres`, and the two
    differ only in how the same sentence is delivered. A function that is called
    raises; a fixture that cannot supply what it promised skips, because a test
    that never ran is honest and a test that ran against a database nobody
    shaped is not. Silently yielding an unbuilt world would be the vacuous pass
    this package exists to expose, and returning a warning would be one nobody
    reads.

    The reason is the refusal's own message rather than a shorter one written
    here, so the skip line names the same three things the exception does: what
    was refused, which connection, and what that connection actually is.

    Public because a consumer writing its own fixture over a shaped database
    needs exactly this, and because a plan assertion in a downstream package has
    the same obligation to skip rather than pass.
    """
    try:
        require_postgres(connection, operation)
    except UnsupportedBackend as unsupported:
        pytest.skip(str(unsupported))
