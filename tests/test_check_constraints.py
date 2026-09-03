"""Refusing a declaration the models' own constraints could not hold.

Every refusal here is one the database would also make, and would make with a
unique index failing at row N of a load that has already run for a minute. So
what is being tested is not that the data is impossible -- PostgreSQL settles
that -- but that it is said so before a row is generated, and said with the
arithmetic in it.
"""

from __future__ import annotations

import datetime
from typing import Any, cast

import pytest

from django_data_shape import (
    Constant,
    Derived,
    FanOut,
    InvalidShape,
    PerParent,
    Projection,
    Sequential,
    Shape,
    Skew,
    Table,
    Uniform,
    Zipf,
)
from tests.testapp.models import (
    Assignment,
    Booking,
    Company,
    Contest,
    Event,
    EventSession,
    Invitation,
    Membership,
    Person,
    Project,
    Seat,
    Template,
    TemplateSession,
    Venue,
)

_START = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)

_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def _letter_for_position(group: object) -> str:
    """One letter per position inside a parent's group of children.

    The shape of a ``Scope.GROUP`` derivation that *does* keep a two-column
    uniqueness, which is why the refusal names this form: the source resolves to
    ``(position, size)``, so a value can be chosen per position rather than
    drawn beside the fan-out.
    """
    position, _size = cast("tuple[int, int]", group)
    return _LETTERS[position % len(_LETTERS)]


def _companies(rows: int = 50) -> Table:
    return Table(Company, rows=rows, name=Constant("acme"))


def _projects(rows: int = 2000, **overrides: Any) -> Table:
    fields: dict[str, Any] = {
        "company": FanOut(Zipf(1.2)),
        "created_at": Sequential(_START, datetime.timedelta(minutes=1)),
        "status": PerParent("company", last="ACTIVE", rest="COMPLETE"),
    }
    fields.update(overrides)
    return Table(Project, rows=rows, fields=fields)


def test_a_skew_beside_a_fan_out_under_a_partial_constraint_is_refused() -> None:
    # The worked example, and the whole reason this pre-check exists. Fifty
    # companies permit fifty active projects; a tenth of two thousand rows asks
    # for two hundred.
    with pytest.raises(InvalidShape, match="one_active_project_per_company") as raised:
        Shape(
            _companies(50),
            _projects(2000, status=Skew({"ACTIVE": 0.1, "COMPLETE": 0.9})),
        )

    message = str(raised.value)
    assert "at most 50 rows with status='ACTIVE'" in message
    assert "asks for 200 of them" in message
    assert "PerParent('company', last='ACTIVE'" in message


def test_the_refusal_holds_at_a_share_that_would_have_fitted() -> None:
    # 2.5% of two thousand rows is fifty, which is exactly the capacity -- and
    # it is still refused, because a rule about a group cannot be kept by a
    # draw made per row. A smaller share only moves the collision later into
    # the load, which is the failure that costs a minute to hear about.
    with pytest.raises(InvalidShape, match="at any weight"):
        Shape(
            _companies(50),
            _projects(2000, status=Skew({"ACTIVE": 0.025, "COMPLETE": 0.975})),
        )


def test_a_declaration_that_never_writes_the_value_is_accepted() -> None:
    # Constant enumerates itself, so this is decidable rather than merely
    # unproven: a table with no active projects at all keeps the rule, and
    # refusing it would be the pre-check inventing a problem.
    Shape(_companies(50), _projects(2000, status=Constant("COMPLETE")))


def test_per_parent_over_the_constraints_own_field_is_accepted() -> None:
    Shape(_companies(50), _projects(2000))


def test_per_parent_grouped_by_something_the_constraint_does_not_group_by() -> None:
    # Two parents, and the rule is kept per the wrong one. A lead per contest
    # says nothing at all about how many leads a company ends up with, and the
    # table's own checks cannot see it: both relations are declared fan-outs.
    with pytest.raises(InvalidShape, match="says nothing about how many rows"):
        Shape(
            _companies(50),
            Table(Contest, rows=10, name=Constant("cup")),
            Table(
                Assignment,
                rows=2000,
                company=FanOut(Zipf()),
                contest=FanOut(Zipf()),
                role=PerParent("contest", last="LEAD", rest="MEMBER"),
            ),
        )


def test_several_winners_per_group_under_a_unique_constraint_is_refused() -> None:
    with pytest.raises(InvalidShape, match="count= is what N-winners-per-contest is for"):
        Shape(
            _companies(50),
            _projects(
                2000,
                status=PerParent("company", last="ACTIVE", rest="COMPLETE", count=3),
            ),
        )


def test_a_per_parent_whose_rest_writes_the_constrained_value_is_refused() -> None:
    # The special value is a different one, so the constraint's value can only
    # arrive through the rest of the group -- where it arrives in every row.
    with pytest.raises(InvalidShape, match="one_active_project_per_company"):
        Shape(
            _companies(50),
            _projects(
                2000,
                status=PerParent("company", last="ARCHIVED", rest="ACTIVE"),
            ),
        )


def test_a_per_parent_whose_rest_never_writes_it_is_accepted() -> None:
    Shape(
        _companies(50),
        _projects(2000, status=PerParent("company", last="ARCHIVED", rest="COMPLETE")),
    )


def test_a_per_parent_whose_rest_distribution_never_writes_it_is_accepted() -> None:
    Shape(
        _companies(50),
        _projects(
            2000,
            status=PerParent(
                "company",
                last="ARCHIVED",
                rest=Skew({"COMPLETE": 0.9, "CANCELLED": 0.1}),
            ),
        ),
    )


def test_a_per_parent_whose_rest_distribution_writes_it_is_refused() -> None:
    with pytest.raises(InvalidShape, match="one_active_project_per_company"):
        Shape(
            _companies(50),
            _projects(
                2000,
                status=PerParent(
                    "company",
                    last="ARCHIVED",
                    rest=Skew({"COMPLETE": 0.9, "ACTIVE": 0.1}),
                ),
            ),
        )


def test_a_parent_this_shape_does_not_build_leaves_the_number_out() -> None:
    # The honest half of "declaration time": whether to refuse is decidable
    # without the parent's row count, and the arithmetic in the message is not.
    # A project that builds its fifty companies with the ORM gets the refusal
    # and gets it phrased per group.
    with pytest.raises(InvalidShape, match="at most one row with status='ACTIVE'") as raised:
        Shape(_projects(2000, status=Skew({"ACTIVE": 0.1, "COMPLETE": 0.9})))

    assert "at most 50" not in str(raised.value)


def test_a_distribution_that_cannot_enumerate_itself_is_still_refused() -> None:
    # Undecidable in the other direction: nothing can ask a Uniform whether it
    # emits the value, so the refusal stands and drops the arithmetic.
    with pytest.raises(InvalidShape, match="draws 'ACTIVE' independently per row"):
        Shape(_companies(50), _projects(2000, status=Uniform(0, 1, places=0)))


def test_a_two_column_uniqueness_short_of_combinations_is_refused() -> None:
    # Pigeonhole, and provable however the seed falls -- which is why it is
    # refused where a table alone declined to guess. Only a whole shape knows
    # how many companies there are.
    with pytest.raises(InvalidShape, match="one_seat_label_per_company") as raised:
        Shape(
            _companies(50),
            Table(Seat, rows=2000, company=FanOut(Zipf()), label=Skew({"a": 1, "b": 1})),
        )

    message = str(raised.value)
    assert "needs 2000 distinct (company, label) combinations" in message
    assert "can produce 100" in message


def test_the_same_uniqueness_with_room_for_every_row_is_still_refused() -> None:
    # Room, and nothing to arrange it. Fifty companies and two labels hold
    # exactly a hundred pairs, so every arithmetic check passes -- and the load
    # died inside COPY at row 17, because a group of three rows draws from two
    # labels whatever the table's total capacity says.
    with pytest.raises(InvalidShape, match="one_seat_label_per_company") as raised:
        Shape(
            _companies(50),
            Table(Seat, rows=100, company=FanOut(Zipf()), label=Skew({"a": 1, "b": 1})),
        )

    message = str(raised.value)
    assert "Seat.company is a fan-out beside Seat.label=Skew({'a': 1, 'b': 1})" in message
    assert "The 100 combinations do fit the 100 rows" in message
    # And it names the one primitive that can be arranged around a group,
    # rather than leaving a reader to discover it.
    assert "Derived('company', compute=..., scope='group')" in message


def test_a_null_share_makes_the_capacity_unknown_rather_than_smaller() -> None:
    # PostgreSQL counts each NULL in a unique index as distinct, so those rows
    # are exempt from the constraint entirely -- a capacity computed as though
    # they were not would name a number that is not true of this shape. The
    # rows that do have a parent are still unarranged, so the refusal stands
    # and it is the one that carries no arithmetic.
    with pytest.raises(InvalidShape, match="There may be room for every row") as raised:
        Shape(
            _companies(50),
            Table(
                Seat,
                rows=2000,
                company=FanOut(Zipf(), null=0.1),
                label=Skew({"a": 1, "b": 1}),
            ),
        )

    assert "do fit" not in str(raised.value)


def test_a_column_no_distribution_bounds_makes_the_capacity_unknown() -> None:
    # Nothing can ask a Uniform how many values it emits, so the pigeonhole has
    # no number to compare -- and unbounded is not the same as distinct. Two of
    # one company's rows landing on the same integer is a coin flip, and the
    # measured one comes up heads about half the time.
    with pytest.raises(InvalidShape, match="There may be room for every row"):
        Shape(
            _companies(50),
            Table(
                Seat,
                rows=2000,
                company=FanOut(Zipf()),
                label=Uniform(0, 1_000_000, places=0),
            ),
        )


def test_a_column_distinct_in_every_row_keeps_the_constraint_on_its_own() -> None:
    # The exemption, and the only one that needs no coordination: a pair is
    # distinct as soon as either half is. Sequential says so through Distinct,
    # and this declaration loads -- see test_build_invariants.
    Shape(
        _companies(50),
        Table(Seat, rows=100, company=FanOut(Zipf()), label=Sequential(0, 1)),
    )


def test_a_sequential_that_does_not_move_is_not_distinct() -> None:
    # A zero step writes one value in every row, which is a Constant spelled
    # the long way -- so the protocol has to answer for the parameters and not
    # for the class, and the refusal has to come back.
    with pytest.raises(InvalidShape, match="one_seat_label_per_company"):
        Shape(
            _companies(50),
            Table(Seat, rows=100, company=FanOut(Zipf()), label=Sequential(0, 0)),
        )


def test_a_column_derived_from_the_group_is_left_to_the_other_two_nets() -> None:
    # The exemption that is the point rather than a gap. A derivation reads
    # something other than its own row index, so it is the one kind of
    # declaration that can be arranged around a group -- and whether a
    # particular compute= is arranged around it is not readable here.
    Shape(
        _companies(50),
        Table(
            Seat,
            rows=100,
            company=FanOut(Zipf()),
            label=Derived("company", compute=_letter_for_position, scope="group"),
        ),
    )


def test_every_constraint_this_cannot_read_is_left_to_the_other_two_nets() -> None:
    # Six skips in one model: a condition that is not a single equality, one
    # written over an expression rather than fields, one grouped by a column no
    # fan-out partitions, one joining two clauses, one whose single clause is a
    # nested Q, and a check constraint, which is not a unique constraint at all.
    #
    # seats is Constant(0) against a `seats__gt=0` condition on purpose: read as
    # an equality that condition would be decided, and decided as a refusal, so
    # a suffix check that stopped telling a lookup from an equality fails here
    # rather than passing quietly.
    Shape(
        _companies(50),
        Table(
            Booking,
            rows=2000,
            company=FanOut(Zipf()),
            room=Skew({"one": 1, "two": 1}),
            state=Skew({"HELD": 0.5, "PAID": 0.5}),
            seats=Constant(0),
        ),
    )


def test_a_condition_on_a_column_this_shape_leaves_undeclared_is_skipped() -> None:
    # Nullable and undeclared, so every row holds the same nothing and the
    # constraint has nothing to weigh.
    Shape(
        _companies(50),
        Table(Invitation, rows=2000, company=FanOut(Zipf()), label=Constant("x")),
    )


def test_a_projection_is_skipped_entirely() -> None:
    # Its columns are copied along a join rather than drawn, so there is no
    # declared share for a capacity to be compared against.
    shape = Shape(
        Table(Template, rows=5, name=Constant("t")),
        Table(Venue, rows=2, name=Constant("v")),
        Table(
            TemplateSession,
            rows=20,
            template=FanOut(Constant(1)),
            title=Constant("s"),
            minutes=Constant(30),
        ),
        Table(
            Event,
            rows=10,
            template=FanOut(Constant(1)),
            venue=FanOut(Constant(1)),
            name=Constant("e"),
        ),
        Projection(EventSession, per=Event, copying=TemplateSession),
    )

    assert len(shape.tables) == 5


def test_two_fan_outs_under_one_uniqueness_are_refused_by_name() -> None:
    # The through table of a many-to-many, and the case the arithmetic cannot
    # see: twenty companies and twenty people leave four hundred pairs for two
    # hundred rows, so the pigeonhole check passes it and the load dies inside
    # COPY on a unique violation, at a row number that moves with the seed.
    # Room was never the question -- nothing enumerates the combinations.
    with pytest.raises(InvalidShape, match="one_membership_per_company_person") as raised:
        Shape(
            _companies(20),
            Table(Person, rows=20, name=Constant("p")),
            Table(
                Membership,
                rows=200,
                company=FanOut(Zipf()),
                person=FanOut(Zipf()),
                role=Constant("member"),
            ),
        )

    message = str(raised.value)
    assert "Membership.company, Membership.person are fan-outs" in message
    # And it points at the form that does build one today, rather than leaving a
    # reader to discover that the escape hatch is also the answer.
    assert "Projection(Membership, columns=(...), sql=...)" in message


def test_a_single_fan_out_under_a_uniqueness_says_which_columns_it_means() -> None:
    # One fan-out and one drawn column is the same defect one column over, and
    # it gets its own message rather than the two-fan-out one: the obstruction
    # is the same and the remedy is not.
    with pytest.raises(InvalidShape, match="one_seat_label_per_company") as raised:
        Shape(
            _companies(50),
            Table(Seat, rows=100, company=FanOut(Zipf()), label=Skew({"a": 1, "b": 1})),
        )

    message = str(raised.value)
    assert "are fan-outs" not in message
    assert "Projection(Seat" not in message


def test_the_pigeonhole_arithmetic_is_still_what_answers_first() -> None:
    # Both refusals apply to this declaration, and the arithmetic is the more
    # useful of the two: it says the shape does not fit at all, which is a
    # different instruction from "it fits and nothing arranges it".
    with pytest.raises(InvalidShape, match="can produce 25") as raised:
        Shape(
            _companies(5),
            Table(Person, rows=5, name=Constant("p")),
            Table(
                Membership,
                rows=200,
                company=FanOut(Zipf()),
                person=FanOut(Zipf()),
                role=Constant("member"),
            ),
        )

    assert "fan-outs" not in str(raised.value)
