"""How many children each parent ended up with, and which parents got none."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence


class ChildrenPerParent(Mapping[object, int]):
    """The realised fan-out: one child count per parent key.

    The inversion a :class:`~django_data_shape.fan_out.FanOut` exists to make
    possible, in the only coordinate a caller actually holds. Internally the
    partition is over parent *positions* and child *row indices*, neither of
    which appears in any column; this is keyed on the **parent's primary key**,
    which is the value sitting in the child's foreign key column and the value a
    test already has in its hand.

    It is a plain read-only mapping, so ``counts[company.pk]``, ``len(counts)``,
    ``sum(counts.values())`` and iteration all work without anything being
    invented for them. What is added is the pair of questions that are the whole
    reason to declare a skew and the only ones an aggregate over the child table
    could otherwise answer: which parents are the head, and which have no
    children at all.

    **The counts are the partition.** With the default ``null=0`` that is
    exactly how many child rows point at each parent. A fan-out declaring a null
    share thins the partition per row *after* it is computed, so under one of
    those the counts are an upper bound on the rows actually pointing at each
    parent, and the thinning is uniform in expectation rather than exact --
    :attr:`null_share` is the share it was thinned by, and it is zero whenever
    these numbers are row counts. The ranking is unaffected either way, which is
    what the head and the tail are read for.

    Iteration order is the order the parent keys were read, which is the parent
    table's own primary-key order. :meth:`ranked` is the other order, and the
    two are kept separate for the reason every pair of orders in this package is:
    conflating them is how "the largest group" quietly becomes "the first group".
    """

    def __init__(self, parents: Sequence[object], sizes: Sequence[int], null_share: float) -> None:
        # Zipped strictly: the two come out of one partition and a length
        # mismatch would mean the sizes had been computed over different
        # parents from the ones being reported, which is the one error this
        # class must not paper over by truncating.
        self._counts = dict(zip(parents, sizes, strict=True))
        self._null_share = null_share
        # Both orders are computed once, here, rather than on each call. The
        # object is inert data handed to a test, a test reads the head and the
        # tail from the same object, and sorting fifty thousand parents twice
        # for that is work nobody asked for. Sorting is stable, so ties keep the
        # parent-key order the rows were read in and the answer is reproducible
        # rather than merely deterministic-in-this-interpreter.
        self._ranked = tuple(sorted(self._counts.items(), key=lambda item: item[1], reverse=True))
        self._childless = tuple(parent for parent, count in self._counts.items() if count == 0)

    def __getitem__(self, parent: object) -> int:
        return self._counts[parent]

    def __iter__(self) -> Iterator[object]:
        return iter(self._counts)

    def __len__(self) -> int:
        return len(self._counts)

    @property
    def null_share(self) -> float:
        """The share of children whose foreign key is null, from the declaration.

        Zero for a fan-out that declared none, which is the case where these
        counts are row counts rather than an upper bound on them. Exposed rather
        than left in the declaration because a caller comparing a count here
        against ``Child.objects.filter(parent=p).count()`` needs to be able to
        see, from the object that gave them the number, why the two differ.
        """
        return self._null_share

    def ranked(self) -> tuple[tuple[object, int], ...]:
        """Every parent and its count, most children first.

        The head of the distribution is the front of this and the tail is the
        back, so one method answers both rather than two answering one each with
        a count argument to get wrong. A test wanting the busiest parent takes
        ``ranked()[0]``; one wanting the five busiest takes ``ranked()[:5]``.

        Returned whole rather than sliced here because the interesting question
        is rarely only the first row: a plan assertion usually wants the busiest
        parent *and* a parent from the middle, to show that one query plan is
        chosen for the head and another for the body.
        """
        return self._ranked

    def childless(self) -> tuple[object, ...]:
        """The parents with no children at all, in parent-key order.

        Its own method rather than a filter over :meth:`ranked` because it is
        the case a hand-written fixture never has and the one that changes what
        a query returns: a parent nobody references is the difference between an
        inner join and an outer join giving the same answer and giving different
        ones. A test for that behaviour needs a parent that is genuinely
        unreferenced, and guessing one from the tail of the ranking is how it
        ends up testing a parent with three children.
        """
        return self._childless

    def __repr__(self) -> str:
        return (
            f"ChildrenPerParent({len(self._counts)} parents, {sum(self._counts.values())} children)"
        )
