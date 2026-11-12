from __future__ import annotations


class SequentialKeys:
    """``row + 1``: the default for any integer primary key.

    Counting from one rather than zero because that is what a database sequence
    does, and a test database whose keys start at zero is subtly unlike every
    other one the reader has seen.

    This is the strategy that leaves the identity sequence behind the rows, so
    the load resets it afterwards: the keys sit at 1..N with the sequence at 1.
    A key type with no sequence behind it has nothing to move.

    It can say itself in SQL, so it can fill a
    :class:`~django_data_shape.declaration.projection.Projection` -- see
    :class:`~django_data_shape.keys.sql_keys.SqlKeys`. ``row + 1`` is arithmetic
    every database has, and a keyed hash is not: of the UUID strategies only
    :class:`~django_data_shape.keys.md5_keys.Md5Keys` has a SQL twin.
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

    def canonical(self) -> object:
        """Nothing to say: the rule is ``row + 1`` and has no parameters. See ``Canonical``.

        The empty tuple is not the same as being absent -- the strategy's own
        type name is part of the digest, so this differs from ``UuidKeys()``,
        which also has nothing to say.
        """
        return ()

    def __repr__(self) -> str:
        return "SequentialKeys()"
