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

    # Applications store v4 keys, so a merely UUID-shaped key would be unlike the real thing.
    assert all(isinstance(key, uuid.UUID) for key in made)
    assert {key.version for key in made} == {4}
    assert {key.variant for key in made} == {uuid.RFC_4122}


def test_the_same_seed_and_row_give_the_same_key() -> None:
    assert Md5Keys().key_for(3, stream=7) == Md5Keys().key_for(3, stream=7)
    assert Md5Keys().key_for(3, stream=7) != Md5Keys().key_for(3, stream=8)


def test_it_is_a_different_strategy_and_not_a_second_meaning_for_uuid_keys() -> None:
    # Substituting it for a SQL twin would change every key already built, with two meanings.
    assert Md5Keys().key_for(0, 1) != UuidKeys().key_for(0, 1)
    assert repr(Md5Keys()) == "Md5Keys()"
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
    """The fixture is the assertion, so it is checked rather than trusted."""
    signed_bigint_max = 2**63 - 1

    assert _STREAMS[0] <= signed_bigint_max < _STREAMS[1]


@pytest.mark.django_db
@pytest.mark.skipif(connection.vendor != "postgresql", reason="the SQL half needs PostgreSQL")
@pytest.mark.parametrize("stream", _STREAMS)
def test_the_two_halves_agree_for_a_stream_postgres_cannot_hold_as_bigint(stream: int) -> None:
    """The bug a consumer found, and the reason it was never caught here."""
    keys = Md5Keys()

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {keys.key_sql(stream, 'g')} FROM generate_series(0, 19) AS g")
        from_sql = [row[0] for row in cursor.fetchall()]

    assert from_sql == [keys.key_for(row, stream) for row in range(20)]


@pytest.mark.django_db
@pytest.mark.skipif(connection.vendor != "postgresql", reason="the SQL half needs PostgreSQL")
def test_the_two_halves_agree_against_a_real_server() -> None:
    """The only form of agreement that means anything for a SQL twin."""
    keys = Md5Keys()
    stream = 12345

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {keys.key_sql(stream, 'g')} FROM generate_series(0, 49) AS g")
        from_sql = [row[0] for row in cursor.fetchall()]

    assert from_sql == [keys.key_for(row, stream) for row in range(50)]
