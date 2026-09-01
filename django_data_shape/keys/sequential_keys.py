"""Dense integer keys, counting from one."""

from __future__ import annotations


class SequentialKeys:
    """``row + 1``: the default for any integer primary key.

    Counting from one rather than zero because that is what a database sequence
    does, and a test database whose keys start at zero is subtly unlike every
    other one the reader has seen.

    This is the strategy that obliges the sequence reset after loading. It is
    also the only one that does: a key type with no sequence behind it has
    nothing to move.
    """

    def key_for(self, row: int, stream: int) -> object:
        return row + 1

    def __repr__(self) -> str:
        return "SequentialKeys()"
