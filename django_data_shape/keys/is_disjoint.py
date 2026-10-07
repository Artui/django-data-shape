"""Whether a key strategy says its keys cannot collide with rows already there."""

from __future__ import annotations

from django_data_shape.keys.disjoint import Disjoint


def is_disjoint(keys: object) -> bool:
    """Whether ``keys`` implements :class:`~django_data_shape.keys.disjoint.Disjoint` and says yes.

    The one reading of the protocol, shared by the two places that act on it:
    ``build``, which builds such a table beside the rows already there rather
    than refusing it, and ``scaled_world``, which leaves its rows in place
    rather than emptying the table and draws its keys from a stream of the
    world's own. They read it separately once, and parted on a strategy that
    implements the protocol and answers no: the world left its rows in place
    and the build then refused it for holding them.

    Implementing the protocol is not enough, because the answer can be a
    property of a strategy's parameters and not only of its class. Without
    the call, ``test_a_strategy_that_says_it_is_not_is_not`` fails, and in a
    world so does ``test_a_strategy_that_says_it_is_not_disjoint_is_emptied``.
    Without the ``isinstance``, a strategy that does not implement it --
    :class:`~django_data_shape.keys.key_function.KeyFunction`, whose function
    this package cannot read -- raises ``AttributeError`` rather than being
    read as not disjoint, and ``test_a_strategy_that_does_not_implement_it_is_not``
    fails. ``None`` is read as not disjoint, so a
    :class:`~django_data_shape.declaration.projection.Projection`, which has no
    ``keys`` to ask, can be passed ``getattr(table, "keys", None)``.
    """
    return isinstance(keys, Disjoint) and keys.is_disjoint_from_existing_rows()
