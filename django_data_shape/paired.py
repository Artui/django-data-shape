"""The other end of a many-to-many edge, distinct within its partner's group."""

from __future__ import annotations

from django_data_shape.distributions.distribution import Distribution
from django_data_shape.invalid_shape import InvalidShape


class Paired:
    """A relation filled with the opposite end of an edge, never repeating a pair.

    The declaration a through table needs, and the reason two
    :class:`~django_data_shape.fan_out.FanOut` declarations are refused beside
    one another. Two fan-outs partition the same rows without either seeing the
    other, so nothing enumerates the pairs and whether two rows collide is a
    matter of the seed. ``Paired`` is the half that looks:

    .. code-block:: python

        Table(
            Membership,
            rows=50_000,
            company=FanOut(Zipf()),
            person=Paired("company", Zipf()),
            role=Constant("member"),
        )

    ``company`` partitions the rows as any fan-out does -- that is the **declared**
    degree distribution, and it is exact. Within each of its groups, ``person``
    takes that many **distinct** partners, so a duplicate pair is impossible by
    construction rather than removed afterwards: the same company implies a
    different person, and a different company implies a different pair. The edge
    count is therefore exactly ``rows``, which is the rule this package holds to
    everywhere else and which a deduplicating generator would have given up.

    ``relation`` names the fan-out this one is paired with, spelled the way
    :class:`~django_data_shape.derivations.per_parent.PerParent` names the
    relation it groups by, and for the same reason: a table with three foreign
    keys should not make a reader guess which one the grouping is over.

    ``weights`` decides which partners are popular. It is a
    :class:`~django_data_shape.distributions.distribution.Distribution` over the
    partner table the same way a fan-out's ``sizes`` is over the parent table,
    so ``Zipf()`` gives the realistic heavy tail and a flat one gives the
    uniform shape this package exists to argue against.

    **The second side's marginal is derived, not declared, and that is on
    purpose.** Both marginals plus the edge count is over-determined: fixing all
    three is a constraint satisfaction problem, and a CSP cannot stream into
    ``COPY``. So the edge count and one side's distribution are declared and the
    other side follows -- and what follows *approximates* ``weights`` rather than
    reproducing it. Measured against exact weighted sampling without
    replacement, the busiest partner comes out about 1.1 times as busy, the 99th
    percentile about 30% high, and about 7% fewer partners are touched at all.
    Those numbers are in the documentation rather than only here, because a
    derived shape nobody quotes is a shape nobody can check.
    """

    def __init__(self, relation: str, weights: Distribution) -> None:
        if not isinstance(relation, str) or not relation:
            raise InvalidShape(
                f"Paired needs the name of the fan-out it is paired with, got {relation!r}. "
                "A table with more than one foreign key cannot be left to guess which one the "
                "pairing is over."
            )
        self._relation = relation
        self._weights = weights

    @property
    def relation(self) -> str:
        return self._relation

    @property
    def weights(self) -> Distribution:
        return self._weights

    def canonical(self) -> object:
        """The fan-out it pairs with and the weights over partners. See ``Canonical``."""
        return (self._relation, self._weights)

    def __repr__(self) -> str:
        return f"Paired({self._relation!r}, {self._weights!r})"
