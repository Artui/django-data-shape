from __future__ import annotations


class SequentialKeys:
    """``row + 1``: the default for any integer primary key.

    Counting from one, as a database sequence does, so keys match what production writes."""

    def key_for(self, row: int, stream: int) -> object:
        return row + 1

    def key_sql(self, stream: int, row: str) -> str:
        """The same ``row + 1``, for a row index the database computes."""
        return f"({row}) + 1"

    def canonical(self) -> object:
        """Nothing to say: the rule is ``row + 1`` and has no parameters. See ``Canonical``."""
        return ()

    def __repr__(self) -> str:
        return "SequentialKeys()"
