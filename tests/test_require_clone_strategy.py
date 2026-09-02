"""The strategy gate, covered by passing a version rather than by installing one."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from django_data_shape import UnsupportedBackend
from django_data_shape.require_clone_strategy import STRATEGIES, require_clone_strategy


@dataclass
class _Connection:
    """Everything the gate reads, and nothing else.

    A stub rather than a second server, for the reason the vendor gate has one:
    a refusal that could only be covered by running the suite against a
    PostgreSQL 14 is a refusal the coverage gate cannot see, and this package
    gates coverage on PostgreSQL because that is where its real work happens.
    """

    pg_version: int
    alias: str = "default"


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_a_strategy_this_server_understands_passes(strategy: str) -> None:
    require_clone_strategy(_Connection(pg_version=150_000), strategy)


def test_no_strategy_at_all_passes_on_any_version() -> None:
    # None is not a value PostgreSQL has to know about: it means the clause is
    # left off entirely, which is what every version does by default. So the
    # version is never consulted, and an old server has a way through.
    require_clone_strategy(_Connection(pg_version=120_000), None)


def test_a_strategy_nobody_defines_is_refused_before_it_reaches_a_statement() -> None:
    # It would be interpolated into CREATE DATABASE -- the grammar takes no
    # placeholder -- so the allowlist is the thing standing between a caller's
    # typo and arbitrary SQL.
    with pytest.raises(ValueError, match="must be one of file_copy, wal_log"):
        require_clone_strategy(_Connection(pg_version=160_000), "fastest")


def test_a_server_without_the_clause_is_refused_with_the_way_round_it() -> None:
    with pytest.raises(UnsupportedBackend) as raised:
        require_clone_strategy(_Connection(pg_version=140_010, alias="legacy"), "file_copy")

    message = str(raised.value)
    assert "PostgreSQL 15" in message
    assert "legacy" in message
    # A refusal that named no way forward would leave the reader to guess that
    # the clause is optional, which is the whole content of the answer.
    assert "strategy=None" in message
