"""What a declaration is made of, said in data rather than in code."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Canonical(Protocol):
    """A declaration that can describe itself as plain, comparable data.

    The third opt-in protocol in this package, and written for the same reason
    as the other two. :class:`~django_data_shape.distributions.bounded.Bounded`
    lets a distribution say how many values it can produce;
    :class:`~django_data_shape.keys.sql_keys.SqlKeys` lets a key strategy say
    itself in SQL. This one lets any declaration say what it is made of, so a
    whole :class:`~django_data_shape.shape.Shape` can be hashed into a
    template-database cache key -- see
    :func:`~django_data_shape.shape_digest.shape_digest`.

    ``canonical`` returns a tree of plain values: numbers, strings, ``None``,
    ``Decimal``, dates and times, ``UUID``s, enum members, sequences, mappings,
    and other objects implementing this protocol. Anything else is refused when
    the digest is taken, by name, because a value the digest cannot read is a
    value it would have to ignore -- and an ignored value is one that changes
    the data while the cache key stays the same.

    **Order is preserved rather than sorted, and that is deliberate.** A
    :class:`~django_data_shape.distributions.skew.Skew` walks its weights in
    declaration order to place the cumulative bounds, so two skews with the same
    weights in different orders assign different values to the same draw. A
    digest that sorted them would call two different databases one, which is the
    one direction this must never be wrong in. Where an order genuinely does not
    reach the data -- a table's field mapping, which is sorted before it becomes
    a ``COPY`` column list -- the implementation sorts, and says so.

    **What deliberately does not implement it.**
    :class:`~django_data_shape.derivations.derived.Derived` and
    :class:`~django_data_shape.keys.key_function.KeyFunction` each wrap a
    callable the caller supplied, and a callable is code rather than data. Its
    name is not its behaviour: two lambdas are both ``<lambda>``, and even a
    function hashed byte for byte can change what it returns when a constant it
    reads is edited somewhere else. A digest that agreed while the data changed
    would serve a stale database, so those two are refused by name instead --
    the same choice ``SqlKeys`` makes for a projected table's keys, and for the
    same reason: approximating gives one declaration two meanings.

    A consumer whose own distribution, derivation or key strategy *is* pure data
    implements this and joins in; one that wraps a callable should not, and the
    refusal will name it.
    """

    def canonical(self) -> object: ...
