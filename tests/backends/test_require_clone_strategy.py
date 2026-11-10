from __future__ import annotations

from dataclasses import dataclass

import pytest

from django_data_shape import UnsupportedBackend
from django_data_shape.backends.require_clone_strategy import STRATEGIES, require_clone_strategy


@dataclass
class _Connection:
    """Everything the gate reads, and nothing else."""

    pg_version: int
    alias: str = "default"


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_a_strategy_this_server_understands_passes(strategy: str) -> None:
    require_clone_strategy(_Connection(pg_version=150_000), strategy)


def test_no_strategy_at_all_passes_on_any_version() -> None:
    # So the version is never consulted, and an old server has a way through.
    require_clone_strategy(_Connection(pg_version=120_000), None)


def test_a_strategy_nobody_defines_is_refused_before_it_reaches_a_statement() -> None:
    # The allowlist is what stands between a caller's typo and arbitrary SQL in CREATE DATABASE.
    with pytest.raises(ValueError, match="must be one of file_copy, wal_log"):
        require_clone_strategy(_Connection(pg_version=160_000), "fastest")


def test_a_server_without_the_clause_is_refused_with_the_way_round_it() -> None:
    with pytest.raises(UnsupportedBackend) as raised:
        require_clone_strategy(_Connection(pg_version=140_010, alias="legacy"), "file_copy")

    message = str(raised.value)
    assert "PostgreSQL 15" in message
    assert "legacy" in message
    # A refusal that names no way forward leaves the reader to guess the clause is optional.
    assert "strategy=None" in message
