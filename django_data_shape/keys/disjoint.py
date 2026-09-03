"""Key strategies whose keys cannot collide with rows already in the table."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Disjoint(Protocol):
    """A key strategy that cannot produce a key some existing row already holds.

    The opt-in protocol behind one refusal, and the refusal is worth stating
    before the protocol is. Building into a table that already has rows is
    normally refused, because this package assigns keys from 1 every time and a
    second build collides on the primary key -- an ``IntegrityError`` from
    inside ``COPY`` naming an index, which tells a reader nothing.

    **That reasoning is about integer keys, and the refusal was not.** A
    :class:`~django_data_shape.keys.uuid_keys.UuidKeys` table derives a
    128-bit digest per row and cannot collide with anything a caller's factory
    wrote, so the refusal blocked the hybrid the documentation advertises --
    parents made by your own code, children made by this package -- for exactly
    the schemas where UUID keys are the norm.

    A strategy that does not implement this is read as *not* disjoint, which is
    the safe direction and the reason it is opt-in rather than a flag with a
    default. :class:`~django_data_shape.keys.key_function.KeyFunction` is the
    case that decides it: the caller's own function could return anything, this
    package cannot read it, and guessing "probably fine" would trade a clear
    refusal for a load that dies partway through.

    ``is_disjoint_from_existing_rows`` returns a bool rather than the protocol
    being a bare marker, for the reason
    :class:`~django_data_shape.distributions.distinct.Distinct` does: the answer
    can be a property of the parameters and not only of the class.

    **It says nothing about anything but keys.** A unique constraint on some
    other column can still collide with a row that was already there, and a
    business invariant can still be broken by rows this package did not write.
    Both are checked after the load, against the table as it then stands, which
    is the reading that stays true either way.
    """

    def is_disjoint_from_existing_rows(self) -> bool: ...
