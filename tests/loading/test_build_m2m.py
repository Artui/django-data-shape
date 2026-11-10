from __future__ import annotations

import statistics
from collections import Counter

import pytest
from django.db import connection

from django_data_shape import Constant, FanOut, InvalidShape, Paired, Shape, Table, Zipf
from django_data_shape import build as build_shape
from tests.testapp.models import Company, Membership, Person

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="COPY loading and planner statistics need PostgreSQL",
    ),
]


def _world(rows: int = 5000, companies: int = 500, people: int = 2000, seed: int = 3) -> Shape:
    return Shape(
        Table(Company, rows=companies, name=Constant("c")),
        Table(Person, rows=people, name=Constant("p")),
        Table(
            Membership,
            rows=rows,
            company=FanOut(Zipf()),
            person=Paired("company", Zipf()),
            role=Constant("member"),
        ),
        seed=seed,
    )


def test_the_edge_count_is_exactly_what_was_declared() -> None:
    """The rule this package holds everywhere, kept through an M2M."""
    build_shape(_world(rows=5000))

    assert Membership.objects.count() == 5000


def test_no_pair_repeats_which_is_what_the_unique_index_checks() -> None:
    # Counting afterwards says it did not, and says what the pairs were.
    build_shape(_world(rows=5000))
    pairs = list(Membership.objects.values_list("company_id", "person_id"))

    assert len(pairs) == len(set(pairs)) == 5000


def test_the_declared_side_keeps_its_distribution_and_the_other_gets_one() -> None:
    build_shape(_world(rows=5000))
    pairs = list(Membership.objects.values_list("company_id", "person_id"))
    companies = sorted(Counter(a for a, _ in pairs).values(), reverse=True)
    people = sorted(Counter(b for _, b in pairs).values(), reverse=True)

    assert companies[0] > 20 * statistics.median(companies)
    # The declared side is a fan-out and behaves like every other fan-out.
    assert people[0] > 20 * statistics.median(people)


def test_a_partner_table_too_small_for_the_busiest_group_is_refused() -> None:
    # A Zipf puts many edges on one company, which then needs more distinct people than exist.
    with pytest.raises(InvalidShape, match="distinct partners -- more than there are"):
        build_shape(_world(rows=2500, companies=500, people=20))


def test_the_same_shape_builds_the_same_edges_twice() -> None:
    # Every table is emptied, or the pairing reads different partner keys the second time.
    build_shape(_world(rows=2000))
    first = sorted(Membership.objects.values_list("company_id", "person_id"))
    Membership.objects.all().delete()
    Person.objects.all().delete()
    Company.objects.all().delete()
    build_shape(_world(rows=2000))

    assert sorted(Membership.objects.values_list("company_id", "person_id")) == first
