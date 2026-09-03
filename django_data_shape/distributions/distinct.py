"""Distributions that never write the same value twice."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Distinct(Protocol):
    """A distribution whose value differs in every row of the table.

    The sixth opt-in protocol, and the exact dual of
    :class:`~django_data_shape.distributions.bounded.Bounded`: that one says
    *how few different values can this produce*, this one says *it produces a
    different one every time*.

    It exists for one question, and the question is not about capacity.
    A multi-column ``UniqueConstraint`` needs the **tuple** to be distinct, and
    every mechanism this package has for filling a column -- a
    :class:`~django_data_shape.fan_out.FanOut` partition, and every
    :class:`~django_data_shape.distributions.distribution.Distribution` --
    computes its column from the row index and from nothing else. So no column
    can see what another column put in the same row, nothing enumerates the
    tuples, and whether two rows collide is a matter of the seed. That is
    refused by
    :func:`~django_data_shape.check_constraints.check_constraints` rather than
    left to fail inside ``COPY``.

    One kind of column keeps such a constraint anyway, and keeps it without
    coordinating with anything: one whose own values are already distinct. A
    pair is distinct as soon as either half is, so ``(company, invoice_number)``
    is unique for free when ``invoice_number`` is. ``Distinct`` is how a
    distribution says that about itself, and saying it is what separates the
    declaration that builds from the one that is a lottery.

    ``is_distinct_per_row`` returns a bool rather than the protocol being a bare
    marker, for the same reason
    :class:`~django_data_shape.distributions.ascending.Ascending` does: the
    answer is a property of the parameters and not of the class.
    :class:`~django_data_shape.distributions.sequential.Sequential` with a
    non-zero step writes a different value in every row and with a zero step
    writes one value in all of them, and those are the same class.

    Structural and opt-in like every protocol here. A distribution that does not
    implement it is read as *not* distinct, which is the safe direction: the
    worst that costs is a refusal a caller answers by adding one method, where
    the other reading costs a load that dies at a row number which moves when
    the seed does.
    """

    def is_distinct_per_row(self) -> bool: ...
