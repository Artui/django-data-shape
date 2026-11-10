from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator

import pytest
from django.db import connection

from django_data_shape import clone_database, drop_database

# transaction=True because creating and dropping a database cannot happen
# inside the atomic block a plain django_db test wraps everything in.
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="CREATE DATABASE and DROP DATABASE are refused on another backend first",
    ),
]


@pytest.fixture
def plain_database() -> Iterator[Callable[[], str]]:
    """Make empty databases under plain names, and drop every one of them afterwards.

    Only names this fixture generated are dropped, and a plain name is one the
    connection's quoting carries intact, so the cleanup cannot reach a database
    the test did not make -- including when a refusal under test did not happen
    and a statement dropped or recreated one of them.
    """
    made: list[str] = []

    def make() -> str:
        name = f"shape_name_{secrets.token_hex(4)}"
        made.append(name)
        with connection._nodb_cursor() as cursor:
            cursor.execute(f"CREATE DATABASE {connection.ops.quote_name(name)}")
        return name

    yield make
    for name in reversed(made):
        drop_database(name)


def _oid(database: str) -> int | None:
    """The database's identity, which changes if it is dropped and made again."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT oid FROM pg_database WHERE datname = %s", [database])
        row = cursor.fetchone()
    return None if row is None else int(row[0])


@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param('"{}"', id="wrapped in double quotes"),
        pytest.param('{}" WITH (FORCE) --', id="carrying a clause after one"),
    ],
)
def test_dropping_a_name_holding_a_double_quote_is_refused_before_anything_is_dropped(
    spelling: str, plain_database: Callable[[], str]
) -> None:
    # The database checked and the database dropped are two different ones.
    made = plain_database()
    before = _oid(made)

    with pytest.raises(ValueError, match="The database name .* contains a double quote"):
        drop_database(spelling.format(made))

    assert before is not None
    assert _oid(made) == before


@pytest.mark.parametrize("which", ["template", "target"])
def test_cloning_with_a_name_holding_a_double_quote_is_refused_before_any_statement(
    which: str, plain_database: Callable[[], str]
) -> None:
    # replace=True, so the first statement would be DROP DATABASE IF EXISTS of the target.
    names = {"template": plain_database(), "target": plain_database()}
    target = names["target"]
    before = _oid(target)
    names[which] = f'"{names[which]}"'

    with pytest.raises(ValueError, match=f"The {which} database name .* contains a double quote"):
        clone_database(names["template"], names["target"], replace=True)

    assert before is not None
    assert _oid(target) == before
