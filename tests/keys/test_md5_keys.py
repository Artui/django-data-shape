"""UUID keys with a SQL twin, so a projection can carry them."""

from __future__ import annotations

import uuid

import pytest
from django.db import connection

from django_data_shape import Md5Keys, UuidKeys
from django_data_shape.keys.disjoint import Disjoint
from django_data_shape.keys.sql_keys import SqlKeys


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
