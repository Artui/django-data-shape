from __future__ import annotations

import uuid

from django_data_shape import UuidKeys


def test_it_produces_well_formed_version_four_uuids() -> None:
    key = UuidKeys().key_for(0, stream=1234)

    # Applications store v4 keys, so a merely UUID-shaped key would be unlike the real thing.
    assert isinstance(key, uuid.UUID)
    assert key.version == 4
    assert key.variant == uuid.RFC_4122


def test_the_same_seed_and_row_give_the_same_key() -> None:
    # A random key would make every foreign key differ between two builds of one shape.
    assert UuidKeys().key_for(41, 99) == UuidKeys().key_for(41, 99)


def test_different_rows_and_streams_give_different_keys() -> None:
    keys = {UuidKeys().key_for(row, 7) for row in range(5000)}

    assert len(keys) == 5000
    assert UuidKeys().key_for(0, 7) != UuidKeys().key_for(0, 8)


def test_it_reads_back_as_what_it_is() -> None:
    assert repr(UuidKeys()) == "UuidKeys()"
