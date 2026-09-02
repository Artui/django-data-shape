"""Distributions that can say whether they climb with the row index."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Ascending(Protocol):
    """A distribution that can say whether its values rise with the row index.

    The fifth opt-in protocol, and the narrowest. It exists for one parameter:
    :class:`~django_data_shape.derivations.per_parent.PerParent`'s ``order_by``,
    which claims that the last row of a group under this column's ordering is
    the last row of the group as the fan-out partitioned it. That claim is true
    only if the column climbs with the row index, and only a distribution can
    say whether it does.

    ``is_ascending`` returns a bool rather than the protocol being a bare
    marker, because the answer is a property of the parameters and not of the
    class. :class:`~django_data_shape.distributions.sequential.Sequential` with
    a positive step climbs and with a negative step falls, and a declaration
    that asked for the *newest* row of each group while filling the column
    backwards would get the oldest -- silently, and in exactly the data nobody
    inspects by hand.

    Structural and opt-in for the reason every protocol here is: inferring the
    answer from the type would refuse a caller's own perfectly monotonic
    distribution, and asserting it on every ``Distribution`` would break the
    ones already written against the single-method protocol.
    """

    def is_ascending(self) -> bool: ...
