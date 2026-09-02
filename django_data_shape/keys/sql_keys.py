"""A key strategy that can also say itself in SQL."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SqlKeys(Protocol):
    """A :class:`~django_data_shape.keys.key_strategy.KeyStrategy` with a SQL twin.

    Every table in a shape has a declared row count, so its keys can be
    enumerated in Python and streamed into ``COPY``. A
    :class:`~django_data_shape.projection.Projection` has no declared row count
    -- its cardinality is determined by the join it copies along -- so there is
    no range of row indices to enumerate, and the rows never pass through Python
    at all. The keys have to be assigned by the statement that inserts them.

    That is the whole reason this protocol exists, and it is deliberately an
    extension of the key strategy rather than a second way to decide keys. A
    projected table's keys come from the same place as every other table's: the
    strategy on the declaration. The only extra requirement is that the strategy
    can express the same rule as an expression the database evaluates.

    ``key_sql`` mirrors
    :meth:`~django_data_shape.keys.key_strategy.KeyStrategy.key_for` argument
    for argument. ``stream`` is the same per-table value derived from the seed.
    ``row`` is a SQL expression that evaluates to the same zero-based row index
    ``key_for`` receives, so an implementation writes the same arithmetic it
    would write in Python -- ``SequentialKeys`` returns ``(row) + 1`` for
    exactly the reason its Python half returns ``row + 1``.

    A strategy that does not implement this is not broken and is not
    second-class; it simply cannot fill a projected table, and it is refused by
    name when one is declared over it. Approximating instead -- a different hash
    in SQL from the one Python computes -- would give one strategy two meanings
    depending on which statement filled the table, which is the quiet divergence
    this package exists to prevent.
    """

    def key_sql(self, stream: int, row: str) -> str: ...
