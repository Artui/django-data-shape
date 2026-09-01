"""How a table's primary keys are decided."""

from __future__ import annotations

from typing import Protocol


class KeyStrategy(Protocol):
    """Turns a row index into that row's primary key.

    The generalisation of what used to be a hard-coded dense ``1..N`` range. The
    range was never the requirement: what the design actually rests on is that
    the key is a **deterministic function of the row index**, and integers were
    only the most obvious such function.

    Everything the dense range bought is bought by determinism instead. A child
    can compute its parent's key from the parent's *index*, so a foreign key is
    satisfied without a lookup whatever the key type. A self-referential tree is
    acyclic because ``parent_index < child_index`` holds on the index, not on the
    value. And two builds of one shape agree because the same seed produces the
    same keys.

    ``stream`` is a per-table value derived from the seed, so a strategy that
    needs entropy has some. One that does not -- a counter, or a caller's own
    function -- ignores it, exactly as a positional distribution ignores its
    draw.
    """

    def key_for(self, row: int, stream: int) -> object: ...
