"""`Paired` narrowed to a subset of partners.

Two through tables over one partner model cannot be made disjoint without this,
so a rule of the form *these two relationships must not overlap* is violated by
construction -- and that is the shape of every separation-of-duties constraint
there is: reviewer-is-not-author, approver-is-not-requester,
auditor-is-not-audited.

`FanOut` gained `parents=` for the same class of problem. This is that gap one
primitive over.
"""

from __future__ import annotations

import pytest

from django_data_shape import (
    Constant,
    FanOut,
    InvalidShape,
    Paired,
    Shape,
    Table,
    Uniform,
    Zipf,
    build,
)
from tests.testapp.models import Company, Membership, Person

pytestmark = pytest.mark.django_db


def _shape(*, first_half: bool, rows: int = 20) -> Shape:
    """Edges over one half of the partner table or the other."""
    partners = list(Person.objects.order_by("id").values_list("id", flat=True))
    chosen = partners[: len(partners) // 2] if first_half else partners[len(partners) // 2 :]
    return Shape(
        Table(
            Membership,
            rows=rows,
            company=FanOut(Uniform(1, 3, places=0)),
            person=Paired("company", Zipf(1.1), parents=chosen),
            role=Constant("member"),
        )
    )


@pytest.fixture
def a_world() -> None:
    build(
        Shape(
            Table(Company, rows=10, name=Constant("c")),
            Table(Person, rows=10, name=Constant("p")),
        ),
        require_statistics=False,
    )


def test_partners_are_drawn_only_from_the_named_subset(a_world: None) -> None:
    allowed = list(Person.objects.order_by("id").values_list("id", flat=True))
    half = allowed[:5]

    build(_shape(first_half=True), require_statistics=False)

    used = set(Membership.objects.values_list("person_id", flat=True))
    assert used
    assert used <= set(half)


def test_two_pairings_over_disjoint_halves_never_overlap(a_world: None) -> None:
    """The rule the whole finding is about, end to end."""
    build(_shape(first_half=True, rows=20), require_statistics=False)
    first = set(Membership.objects.values_list("person_id", flat=True))
    Membership.objects.all().delete()

    build(_shape(first_half=False, rows=20), require_statistics=False)
    second = set(Membership.objects.values_list("person_id", flat=True))

    assert first and second
    assert not (first & second)


def test_a_named_partner_that_matches_no_row_is_refused_by_key(a_world: None) -> None:
    """Silent narrowing is the failure this prevents.

    The database does the narrowing, so a key matching nothing simply does not
    come back and its edges go to whatever else was named -- a world the
    declaration does not describe, built without complaint.
    """
    real = list(Person.objects.order_by("id").values_list("id", flat=True))[:2]

    shape = Shape(
        Table(
            Membership,
            rows=6,
            company=FanOut(Uniform(1, 2, places=0)),
            person=Paired("company", Zipf(), parents=[*real, 9_999_999]),
            role=Constant("member"),
        )
    )

    with pytest.raises(InvalidShape, match="9999999"):
        build(shape, require_statistics=False)


def test_a_subset_too_small_for_the_busiest_group_is_refused(a_world: None) -> None:
    """The constraint `Paired` already enforces, now over the narrowed set.

    A group of size k needs k distinct partners, so narrowing can make a
    declaration impossible that was fine over the whole table.
    """
    only_one = list(Person.objects.order_by("id").values_list("id", flat=True))[:1]
    one_company = list(Company.objects.order_by("id").values_list("id", flat=True))[:1]

    # Every row in one group, so the busiest group needs nine distinct
    # partners and exactly one is on offer.
    shape = Shape(
        Table(
            Membership,
            rows=9,
            company=FanOut(Constant(1), parents=one_company),
            person=Paired("company", Zipf(), parents=only_one),
            role=Constant("member"),
        )
    )

    with pytest.raises(InvalidShape, match="distinct"):
        build(shape, require_statistics=False)


def test_the_declaration_still_digests(a_world: None) -> None:
    """`parents=` is part of what the shape is, so it has to reach the hash."""
    from django_data_shape import shape_digest

    assert shape_digest(_shape(first_half=True)) != shape_digest(_shape(first_half=False))


def test_the_narrowing_shows_in_the_repr() -> None:
    """A repr a reader can paste back into a declaration.

    Without the subset the repr is unchanged, because a declaration that did
    not narrow should not start reading as though it did.
    """
    assert repr(Paired("company", Zipf())) == "Paired('company', Zipf(1.2))"
    assert repr(Paired("company", Zipf(), parents=[1, 2])) == (
        "Paired('company', Zipf(1.2), parents=[1, 2])"
    )
