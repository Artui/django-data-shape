"""Where a derivation's inputs live."""

from __future__ import annotations

from enum import Enum


class Scope(Enum):
    """The one thing that distinguishes one derivation from another.

    A derivation computes a column from something already known. What varies
    between the useful kinds is not *how* the computation runs but **where its
    inputs are read from**, and that is the whole of this enum. It is the reason
    there is one mechanism here rather than four: correlate-with-the-parent,
    correlate-with-a-rank and compute-from-this-row differ by this value alone,
    and by nothing in the resolver.

    ``ROW``
        Sources are other declared columns of the same row, named plainly:
        ``"quantity"``. They are computed first, which is why a derivation
        needs a computation order of its own -- the column order exists to keep
        the ``COPY`` statement stable and says nothing about dependencies.

    ``PARENT``
        Sources are columns of the row on the other side of a declared
        ``FanOut``, named ``"relation.field"``: ``"account.signed_up_at"``.
        They are **read out of the parent table**, not recomputed from the
        parent's declaration, so a parent built with the ORM works exactly like
        one built here. That is the same correction the fan-out itself took:
        the keys are queried rather than assumed, and so are the values beside
        them.

    ``RANK``
        Sources are the names of shared ranks, invented by the declaration:
        ``"size"``. A rank resolves to a draw in [0, 1) that is the same for
        every column naming it, which is what makes two columns extreme in the
        same rows. Ranks are per table and per row, because a rank shared across
        tables would be aligning entities that have nothing to do with each
        other.
    """

    ROW = "row"
    PARENT = "parent"
    RANK = "rank"
