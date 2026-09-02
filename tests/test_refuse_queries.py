"""The rule with teeth: your code may not call the database.

Driven through a stub connection rather than a real one, for the same reason the
backend gate reads a vendor: the wrapper contract is Django's, three lines wide,
and a guard whose refusal could only be covered by a real build is a guard the
coverage gate would push somewhere it means less. The wiring into a real build is
falsified separately, where it is a build test rather than a guard test.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from django_data_shape import DerivationQueriedDatabase
from django_data_shape.refuse_queries import refuse_queries


class _Connection:
    """Django's execute_wrapper contract, and nothing else."""

    def __init__(self) -> None:
        self.wrappers: list[Any] = []

    @contextmanager
    def execute_wrapper(self, wrapper: Any) -> Any:
        self.wrappers.append(wrapper)
        try:
            yield
        finally:
            self.wrappers.pop()


def _wrapper(connection: _Connection) -> Any:
    return connection.wrappers[-1]


def test_a_query_inside_the_block_is_refused() -> None:
    connection = _Connection()

    with (
        refuse_queries(connection, "Ticket", ("total",)),
        pytest.raises(DerivationQueriedDatabase) as raised,
    ):
        _wrapper(connection)(lambda *_: None, "SELECT 1", (), False, {})

    message = str(raised.value)
    assert "Ticket" in message
    assert "total" in message
    assert "SELECT 1" in message


def test_the_guard_is_lifted_when_the_block_ends() -> None:
    connection = _Connection()

    with refuse_queries(connection, "Ticket", ()):
        pass

    # The portable route enters one of these per chunk and executes its insert
    # outside them, so a guard that outlived its block would refuse this
    # package's own statement.
    assert connection.wrappers == []


def test_a_table_with_no_derivations_still_names_what_it_has() -> None:
    connection = _Connection()

    with (
        refuse_queries(connection, "Order", ()),
        pytest.raises(DerivationQueriedDatabase, match="declared on this table are: none"),
    ):
        _wrapper(connection)(lambda *_: None, "SELECT 1", (), False, {})


def test_a_long_statement_is_truncated_rather_than_pasted_whole() -> None:
    connection = _Connection()
    sql = "SELECT " + ", ".join(str(n) for n in range(5000))

    with (
        refuse_queries(connection, "Ticket", ("total",)),
        pytest.raises(DerivationQueriedDatabase) as raised,
    ):
        _wrapper(connection)(lambda *_: None, sql, (), False, {})

    # A generated IN list with ten thousand parameters must not become the
    # error message.
    assert len(str(raised.value)) < 1000


def test_the_wrapper_says_which_model_it_belongs_to() -> None:
    connection = _Connection()

    with refuse_queries(connection, "Ticket", ("total",)):
        # One left behind by an exception is far easier to read this way than as
        # a bare function object.
        assert repr(_wrapper(connection)) == "_Refuse('Ticket')"
