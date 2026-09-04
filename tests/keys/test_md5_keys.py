"""UUID keys with a SQL twin, so a projection can carry them."""

from __future__ import annotations

import uuid

import pytest
from django.db import connection

from django_data_shape import Md5Keys, UuidKeys
from django_data_shape.keys.disjoint import Disjoint
from django_data_shape.keys.sql_keys import SqlKeys
from django_data_shape.utils import field_stream


def test_it_produces_well_formed_version_four_uuids() -> None:
    made = [Md5Keys().key_for(row, stream=7) for row in range(50)]

    # Applications store v4 keys, so something merely UUID-shaped would be
    # unlike the thing this stands in for.
    assert all(isinstance(key, uuid.UUID) for key in made)
    assert {key.version for key in made} == {4}
    assert {key.variant for key in made} == {uuid.RFC_4122}


def test_the_same_seed_and_row_give_the_same_key() -> None:
    assert Md5Keys().key_for(3, stream=7) == Md5Keys().key_for(3, stream=7)
    assert Md5Keys().key_for(3, stream=7) != Md5Keys().key_for(3, stream=8)


def test_it_is_a_different_strategy_and_not_a_second_meaning_for_uuid_keys() -> None:
    # They draw different keys for the same row, so silently substituting one
    # where a SQL twin was needed would change every key in every world already
    # built -- and would give one declaration two meanings depending on which
    # statement filled the table.
    assert Md5Keys().key_for(0, 1) != UuidKeys().key_for(0, 1)
    assert repr(Md5Keys()) == "Md5Keys()"
    # Both have nothing to say about themselves, and that is not them being the
    # same: the strategy's own type name is part of the shape digest, so two
    # shapes differing only in which they use are two different worlds.
    assert Md5Keys().canonical() == UuidKeys().canonical() == ()


def test_only_this_one_can_fill_a_projection() -> None:
    assert isinstance(Md5Keys(), SqlKeys)
    assert not isinstance(UuidKeys(), SqlKeys)


def test_its_keys_cannot_collide_with_rows_the_caller_made() -> None:
    assert isinstance(Md5Keys(), Disjoint)
    assert Md5Keys().is_disjoint_from_existing_rows()


# Streams the producer actually makes, not numbers chosen for a test. The first
# is under PostgreSQL's signed bigint maximum and the second is over it, which
# is the whole of the bug this pair pins: `field_stream` returns an unsigned
# 64-bit value, so about half of all table names land above 2^63 -- a coin flip
# per table rather than anything about a schema. The original test used
# stream=12345, and a hand-picked small number cannot fail this way.
_STREAMS = [
    field_stream(0, "testapp_uuidsession", ":key"),
    field_stream(0, "projects_milestonesubmit", ":key"),
]


def test_the_pair_of_streams_this_module_uses_straddles_the_bigint_limit() -> None:
    """The fixture is the assertion, so it is checked rather than trusted.

    A later change to how streams are derived could quietly move both of these
    below the limit, and the tests below would keep passing while testing
    nothing about the overflow.
    """
    signed_bigint_max = 2**63 - 1

    assert _STREAMS[0] <= signed_bigint_max < _STREAMS[1]


@pytest.mark.django_db
@pytest.mark.skipif(connection.vendor != "postgresql", reason="the SQL half needs PostgreSQL")
@pytest.mark.parametrize("stream", _STREAMS)
def test_the_two_halves_agree_for_a_stream_postgres_cannot_hold_as_bigint(stream: int) -> None:
    """The bug a consumer found, and the reason it was never caught here.

    ``to_hex(<stream>::bigint)`` asks PostgreSQL to re-derive a number Python
    produced as *unsigned*, and bigint is signed -- so the statement raised
    ``NumericValueOutOfRange`` for every seed on any table whose name hashes
    high. The stream is a constant when the statement is built, so nothing has
    to be re-derived at all: the sixteen hex digits are embedded.
    """
    keys = Md5Keys()

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {keys.key_sql(stream, 'g')} FROM generate_series(0, 19) AS g")
        from_sql = [row[0] for row in cursor.fetchall()]

    assert from_sql == [keys.key_for(row, stream) for row in range(20)]


@pytest.mark.django_db
@pytest.mark.skipif(connection.vendor != "postgresql", reason="the SQL half needs PostgreSQL")
def test_the_two_halves_agree_against_a_real_server() -> None:
    """The only form of agreement that means anything for a SQL twin.

    A projection's rows never pass through Python, so the server assigns these
    keys -- which makes a Python half and a SQL half that merely look like the
    same rule worth nothing. They are computed for the same fifty rows and
    compared. A checked-in fixture would prove only that the generator has not
    changed, which is the thing already known.
    """
    keys = Md5Keys()
    stream = 12345

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {keys.key_sql(stream, 'g')} FROM generate_series(0, 49) AS g")
        from_sql = [row[0] for row in cursor.fetchall()]

    assert from_sql == [keys.key_for(row, stream) for row in range(50)]
