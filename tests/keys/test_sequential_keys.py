"""Dense integer keys."""

from __future__ import annotations

from django_data_shape import SequentialKeys


def test_it_counts_from_one() -> None:
    # From one rather than zero: that is what a database sequence does, and a
    # test database whose keys start at zero is subtly unlike every other one.
    assert [SequentialKeys().key_for(row, 0) for row in range(4)] == [1, 2, 3, 4]


def test_the_stream_is_ignored() -> None:
    assert SequentialKeys().key_for(9, 1) == SequentialKeys().key_for(9, 2)


def test_it_reads_back_as_what_it_is() -> None:
    assert repr(SequentialKeys()) == "SequentialKeys()"
