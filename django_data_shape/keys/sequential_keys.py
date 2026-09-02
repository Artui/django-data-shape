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

    It is also the only strategy in this package that can say itself in SQL, so
    it is the only one that can fill a
    :class:`~django_data_shape.projection.Projection` -- see
    :class:`~django_data_shape.keys.sql_keys.SqlKeys`. That is not an accident
    of implementation effort: ``row + 1`` is arithmetic every database has, and
    a keyed hash is not.
    """

    def key_for(self, row: int, stream: int) -> object:
        return row + 1

    def key_sql(self, stream: int, row: str) -> str:
        """The same ``row + 1``, for a row index the database computes.

        Written as the Python expression rather than as ``row_number()``
        directly, so the two halves of this strategy stay visibly the same rule.
        The caller decides what a row index *is* in SQL; this only says what the
        key is, given one.
        """
        return f"({row}) + 1"

    def __repr__(self) -> str:
        return "SequentialKeys()"
