"""The construction: distinct partners per group, exact, with no rejection.

Every property here is one the alternatives failed, so each is asserted rather
than assumed. Exactness is the one the plan for this package had given up on --
it said pairs would be deduped and the build would report an achieved count
against a requested one, which contradicts the rule that cardinality is declared
and never emergent. Nothing is deduped, because nothing can collide.
"""

from __future__ import annotations

import statistics
from collections import Counter

import pytest

from django_data_shape.paired_plan import PairedPlan
from django_data_shape.resolve_paired import _band_of


def _zipf(count: int, exponent: float = 1.2) -> list[float]:
    return [1.0 / (rank**exponent) for rank in range(1, count + 1)]


def _plan(sizes: list[int], partners: int, weights: list[float] | None = None) -> PairedPlan:
    values = weights if weights is not None else _zipf(partners)
    return PairedPlan(
        keys=list(range(partners)),
        sizes=sizes,
        weights=values,
        bands=_band_of(values),
        stream=12345,
    )


def _pairs(plan: PairedPlan, sizes: list[int]) -> list[tuple[int, int]]:
    return [(g, plan.partner_for(g, i)) for g, size in enumerate(sizes) for i in range(size)]


def test_every_group_gets_exactly_the_partners_its_size_asked_for() -> None:
    sizes = [1, 5, 20, 3, 0, 11]
    plan = _plan(sizes, partners=60)

    assert [len({plan.partner_for(g, i) for i in range(s)}) for g, s in enumerate(sizes)] == sizes


def test_no_pair_is_ever_repeated_so_nothing_has_to_be_deduped() -> None:
    # The property the whole construction exists for. Same group implies a
    # different partner, different group implies a different pair -- so the edge
    # count is exactly the row count and no unique constraint can be broken.
    sizes = [7, 3, 19, 1, 12, 8, 4]
    pairs = _pairs(_plan(sizes, partners=40), sizes)

    assert len(pairs) == sum(sizes) == len(set(pairs))


def test_a_group_may_take_every_partner_there_is() -> None:
    # The density the rejection approach degenerated at: a group needing the
    # whole partner table. Here it is the same rule as any other size, because
    # allocation never asks whether a partner is taken.
    plan = _plan([30], partners=30)

    assert len({plan.partner_for(0, i) for i in range(30)}) == 30


def test_the_derived_side_is_skewed_rather_than_flat() -> None:
    """The second marginal is derived, and it still has to be a shape.

    A flat partner side would be the one database in which the join over it
    cannot misestimate, which is the failure this package exists to expose --
    so "derived" must not quietly mean "uniform".
    """
    sizes = [12] * 400
    degrees = sorted(Counter(p for _, p in _pairs(_plan(sizes, 900), sizes)).values(), reverse=True)

    assert degrees[0] > 10 * statistics.median(degrees)


def test_flat_weights_give_a_flat_partner_side() -> None:
    # The control: the skew above comes from the declared weights and not from
    # the construction, so weighing every partner the same has to produce the
    # uniform shape rather than an accidental one.
    sizes = [12] * 400
    plan = _plan(sizes, partners=900, weights=[1.0] * 900)
    degrees = sorted(Counter(p for _, p in _pairs(plan, sizes)).values(), reverse=True)

    assert degrees[0] < 4 * statistics.median(degrees)


def test_the_allocation_totals_the_group_size_even_when_a_band_runs_out() -> None:
    # The spill. A group large enough to exhaust the heavy bands has to take the
    # rest from lighter ones rather than come up short -- which is the step that
    # keeps this exact at high density.
    sizes = [95]
    plan = _plan(sizes, partners=100)

    assert len({plan.partner_for(0, i) for i in range(95)}) == 95


@pytest.mark.parametrize("partners", [1, 2, 3])
def test_a_tiny_partner_table_still_strides(partners: int) -> None:
    # A stride has to be coprime with the band size, and the arithmetic that
    # finds one has no room to search at these sizes.
    plan = _plan([partners], partners=partners)

    assert len({plan.partner_for(0, i) for i in range(partners)}) == partners


def test_the_same_declaration_gives_the_same_edges_twice() -> None:
    sizes = [4, 9, 2]

    assert _pairs(_plan(sizes, 30), sizes) == _pairs(_plan(sizes, 30), sizes)


def test_the_groups_it_reports_are_the_partition_it_was_given() -> None:
    assert _plan([3, 0, 5], partners=10).sizes() == [3, 0, 5]
