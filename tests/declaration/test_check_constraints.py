"""Refusing a declaration the models' own constraints could not hold.

Every refusal here is one the database would also make, and would make with a
unique index failing at row N of a load that has already run for a minute. So
what is being tested is not that the data is impossible -- PostgreSQL settles
that -- but that it is said so before a row is generated, and said with the
arithmetic in it.

**The two unconditional checks interact, and that has broken a test here
already.** The pigeonhole answers first by design -- "this shape does not fit at
all" is a more useful instruction than "it fits and nothing arranges it" -- so a
declaration can be refused by the arithmetic while the arrangement check it was
written for is never reached. A test asserting an *arrangement* refusal has to be
sized so the arithmetic cannot answer: give the drawn column enough distinct
values that the capacity clears the row count, and assert which message came
back. ``Constant`` is the trap, because its capacity is the parent count exactly.
The boundary tests below carry that reasoning inline where they depend on it.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from typing import Any, cast

import django
import pytest
from django.db import connection, models
from django.db.models import Q
from django.db.models.functions import Now
from django.test.utils import isolate_apps

from django_data_shape import (
    Constant,
    Derived,
    FanOut,
    InvalidShape,
    Paired,
    PerParent,
    Projection,
    Sequential,
    Shape,
    Skew,
    Table,
    Tree,
    Uniform,
    Zipf,
)
from django_data_shape import build as build_shape
from tests.declaration.test_a_relation_in_a_condition import (
    _assert_loads,
    _Case,
    _model,
    _named_for_position,
    _paired,
    _refusal,
    _skew,
    _tree,
)
from tests.testapp.models import (
    Approval,
    Assignment,
    Booking,
    Chain,
    Company,
    Contest,
    Coupon,
    Engagement,
    Escalation,
    Event,
    EventSession,
    Folder,
    Invitation,
    Membership,
    Person,
    Project,
    Review,
    Seat,
    Submission,
    Template,
    TemplateSession,
    Ticketed,
    TiedLink,
    Venue,
    Voucher,
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


def test_a_partition_that_gives_no_parent_two_rows_cannot_collide() -> None:
    # The second exemption, and a proof rather than a probability: a collision
    # here is always two rows of one group drawing the same value, so a
    # partition with no group of two cannot produce one. Flat weights make every
    # share rows/parents, and at fifty rows over fifty companies that is exactly
    # one each -- measured as a largest group of one across every parent count
    # up to sixty, every row count at or below it, and five seeds.
    Shape(
        _companies(50),
        Table(Seat, rows=50, company=FanOut(Constant(1)), label=Constant("x")),
    )


def test_the_exemption_holds_below_the_boundary_as_well_as_on_it() -> None:
    # Fewer rows than parents leaves every share below one, so the largest
    # remainder hands out forty-nine single rows and one company gets none.
    Shape(
        _companies(50),
        Table(Seat, rows=49, company=FanOut(Constant(1)), label=Constant("x")),
    )


def test_the_boundary_is_where_the_proof_stops_and_the_refusal_starts() -> None:
    # Two labels rather than one, on purpose, and the pair below is the point.
    # Fifty rows over fifty companies is one seat each and cannot collide;
    # fifty-one gives some company two, and two rows of one group drawing from
    # {a, b} agree half the time. Constant would not test this at all -- its
    # capacity is fifty, so the pigeonhole would answer first and the bound
    # being asserted here would never be reached.
    Shape(
        _companies(50),
        Table(Seat, rows=50, company=FanOut(Constant(1)), label=Skew({"a": 1, "b": 1})),
    )

    with pytest.raises(InvalidShape, match="one_seat_label_per_company") as raised:
        Shape(
            _companies(50),
            Table(Seat, rows=51, company=FanOut(Constant(1)), label=Skew({"a": 1, "b": 1})),
        )

    # The arrangement refusal and not the arithmetic one: a hundred pairs hold
    # fifty-one rows comfortably, so the pigeonhole passes it exactly as it did
    # one row earlier.
    assert "is a fan-out beside" in str(raised.value)


def test_a_childless_share_takes_the_proof_away() -> None:
    # A childless parent is weighed at zero and its rows go to the others, which
    # is what breaks the bound rather than tightening it: at childless=0.1 the
    # largest group over fifty rows and fifty companies is two.
    with pytest.raises(InvalidShape, match="one_seat_label_per_company"):
        Shape(
            _companies(50),
            Table(
                Seat,
                rows=50,
                company=FanOut(Constant(1), childless=0.1),
                label=Constant("x"),
            ),
        )


def test_a_cap_of_one_is_a_proof_whatever_the_sizes() -> None:
    # max_children=1 bounds the partition itself, so none of the other four
    # conditions is needed: skewed sizes, a childless share, and a parent this
    # shape does not build would each take the flat-sizes proof away.
    capped = FanOut(Zipf(), childless=0.3, max_children=1)

    Shape(Table(Seat, rows=50, company=capped, label=Constant("x")))
    Shape(
        _companies(100),
        Table(
            Ticketed,
            rows=50,
            prefix=Constant("p"),
            reference=Sequential("r", "x"),
            company=capped,
        ),
    )


def test_a_cap_of_one_keeps_a_pair_unique_beside_a_second_fan_out() -> None:
    # The composite-unique call site: two fan-outs on one through table are
    # refused unless one of them provably gives no parent two rows, and a cap
    # of one is that proof whatever the other fan-out does.
    Shape(
        _companies(100),
        Table(Person, rows=10, name=Constant("p")),
        Table(
            Membership,
            rows=60,
            company=FanOut(Zipf(), childless=0.3, max_children=1),
            person=FanOut(Zipf(2.0)),
            role=Constant("member"),
        ),
    )


def test_a_cap_of_two_is_not() -> None:
    # The proof is a cap of exactly one; two rows on one parent is all a
    # collision needs.
    with pytest.raises(InvalidShape, match="one_seat_label_per_company"):
        Shape(
            _companies(100),
            Table(
                Seat,
                rows=50,
                company=FanOut(Zipf(), max_children=2),
                label=Constant("x"),
            ),
        )


def test_a_parent_this_shape_does_not_build_takes_the_proof_away_too() -> None:
    # The bound is rows against parents, and a parent loaded by the caller has
    # however many rows it has. A bound resting on a number this package cannot
    # read is not a bound, so the refusal stands.
    with pytest.raises(InvalidShape, match="one_seat_label_per_company"):
        Shape(Table(Seat, rows=50, company=FanOut(Constant(1)), label=Constant("x")))


def test_sizes_that_are_not_provably_flat_are_not_exempt() -> None:
    # Uniform(1, 10) is bounded by nothing this can read and gives a largest
    # group of two at the very numbers Constant(1) gives one. "Usually one each"
    # is not the question -- the exemption is a proof or it is not there.
    with pytest.raises(InvalidShape, match="one_seat_label_per_company"):
        Shape(
            _companies(50),
            Table(
                Seat,
                rows=50,
                company=FanOut(Uniform(1, 10)),
                label=Constant("x"),
            ),
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
    # Seven skips in one model: a condition over an expression rather than
    # fields, one joining two clauses with AND, an OR whose branches name two
    # different columns, a negated OR, an OR one of whose branches is a
    # comparison, a comparison as a keyword argument and the same comparison as
    # a bare lookup expression -- which arrive as different shapes of child and
    # take different exits -- plus a check constraint, which is not a unique
    # constraint at all.
    #
    # Three used to live here and moved out when they stopped being skips:
    # state__in to Review, a nested Q(Q(a) | Q(b)) to Approval, and a condition
    # grouped by a plain column to
    # test_a_condition_grouped_by_a_plain_column_is_refused. What is left
    # is what genuinely describes no set of values for one column -- and the
    # three OR shapes are here to hold that line, because the recursion that
    # reads an OR-tree is the thing most likely to be widened past it.
    #
    # state is Skew({HELD, PAID}) and seats is Constant(0) on purpose: read as
    # sets, those conditions would be decided and decided as refusals, so a
    # decoder that stopped drawing the line here fails rather than passing
    # quietly.
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


def test_a_fan_out_on_a_one_to_one_is_refused() -> None:
    # The instance neither path was checking: a unique=True column makes no
    # entry in _meta.constraints, so this loop never saw it, and Table steps
    # over a fan-out because whether a partition gives any parent two rows
    # depends on the parent's row count -- which one table cannot see and a
    # shape can. Measured 0/20: it does not merely usually fail, it never loads.
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(100),
            Table(
                Ticketed,
                rows=50,
                prefix=Constant("p"),
                reference=Sequential("r", "x"),
                company=FanOut(Zipf()),
            ),
        )

    message = str(raised.value)
    assert "Ticketed.company" in message
    assert "one row at most" in message


def test_a_fan_out_that_cannot_collide_keeps_a_one_to_one() -> None:
    # The same proof the other three refusals carry: a partition giving no
    # parent two rows cannot break a uniqueness on that column either.
    # Measured 20/20 at exactly these numbers.
    Shape(
        _companies(100),
        Table(
            Ticketed,
            rows=50,
            prefix=Constant("p"),
            reference=Sequential("r", "x"),
            company=FanOut(Constant(1)),
        ),
    )


def test_the_two_fan_out_refusal_names_the_pairing_that_replaces_it() -> None:
    # The remedy this refusal named used to be raw SQL, because nothing else
    # could keep the constraint. Paired is that algorithm, so the message points
    # at the declaration first and keeps the statement as the escape hatch.
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(50),
            Table(Person, rows=200, name=Constant("p")),
            Table(
                Membership,
                rows=2000,
                company=FanOut(Zipf()),
                person=FanOut(Zipf()),
                role=Constant("r"),
            ),
        )

    message = str(raised.value)
    assert "Paired('company', Zipf())" in message
    assert "Projection" in message


def test_a_pairing_beside_a_fan_out_keeps_the_constraint_and_is_accepted() -> None:
    """The declaration the refusal above sends a reader to has to be accepted.

    It is exempt for a reason rather than by omission: a pairing takes distinct
    partners inside each group, so no two rows of one group share a partner and
    no two rows of different groups share a pair. That is the same "a pair is
    distinct as soon as either half is" the other exemptions rest on, arrived at
    from the group rather than from the column.
    """
    Shape(
        _companies(50),
        Table(Person, rows=200, name=Constant("p")),
        Table(
            Membership,
            rows=2000,
            company=FanOut(Zipf()),
            person=Paired("company", Zipf()),
            role=Constant("r"),
        ),
    )


def test_a_composite_uniqueness_with_no_fan_out_at_all_is_refused() -> None:
    # The instance the two fan-out refusals do not reach, and the one that shows
    # the fan-out was never what was special. Both columns are drawn per row and
    # nothing partitions either, so nothing enumerates the pairs.
    #
    # Three hundred combinations for fifty rows -- six times the room -- loaded
    # zero runs out of twenty. Ten thousand combinations, two hundred times the
    # room, still failed two.
    with pytest.raises(InvalidShape) as raised:
        Shape(
            Table(
                Coupon,
                rows=50,
                batch=Skew({f"b{i}": 1 for i in range(10)}),
                code=Skew({f"c{i}": 1 for i in range(30)}),
            )
        )

    message = str(raised.value)
    assert "one_code_per_batch" in message
    assert "are all drawn per row" in message
    assert "Distinct" in message


def test_a_distinct_column_keeps_a_composite_uniqueness_with_no_fan_out() -> None:
    # The same exemption the fan-out refusals carry, and for the same one-line
    # reason: a pair is distinct as soon as either half is. Measured 20/20.
    Shape(Table(Coupon, rows=50, batch=Constant("b"), code=Sequential(0, 1)))


def test_a_composite_uniqueness_short_of_combinations_still_says_so_first() -> None:
    # Ordering again: the pigeonhole answers first where it can, because "this
    # does not fit at all" is a different instruction from "it fits and nothing
    # arranges it".
    with pytest.raises(InvalidShape) as raised:
        Shape(Table(Coupon, rows=50, batch=Constant("b"), code=Skew({"c0": 1, "c1": 1})))

    assert "can produce 2" in str(raised.value)


def test_a_column_the_shape_leaves_undeclared_takes_the_refusal_away() -> None:
    # Nullable and undeclared, so every row loads NULL -- and PostgreSQL counts
    # each NULL in a unique index as its own value, so no two rows collide
    # whatever the other column draws. Refusing this would be the pre-check
    # inventing a problem the database does not have.
    Shape(Table(Voucher, rows=50, batch=Skew({f"b{i}": 1 for i in range(10)})))


def test_a_derivation_in_the_constraint_is_left_alone_here_too() -> None:
    # A derivation reads something other than its own row index, which is what
    # makes it the one declaration able to arrange values across rows. Whether a
    # particular compute= does is not readable from here, and refusing on a
    # callable this package cannot read would be the refusal that is wrong.
    Shape(
        Table(
            Coupon,
            rows=50,
            batch=Skew({f"b{i}": 1 for i in range(10)}),
            code=Derived("batch", compute=lambda batch: f"{batch}-x"),
        )
    )


def test_a_condition_spelled_as_a_set_is_read_like_an_equality() -> None:
    # "One open review per company", where open-ness is three statuses rather
    # than one. status is undeclared and comes from the model default, which
    # Table folds into Constant('DRAFT') -- a value inside the set, so every row
    # matches the condition and fifty of them land in one company's group.
    #
    # This was reported by a consumer as the arithmetic only seeing columns the
    # caller declared. It sees the folded default perfectly well; what it could
    # not read was the __in, and the identical shape spelled with = was refused.
    with pytest.raises(InvalidShape) as raised:
        Shape(_companies(50), Table(Review, rows=2000, company=FanOut(Zipf())))

    message = str(raised.value)
    assert "status in ('DRAFT', 'IN_REVIEW', 'APPROVED')" in message
    assert "at most 50 rows" in message
    assert "PerParent('company', last='DRAFT'" in message


def test_a_set_condition_no_declared_value_can_match_is_accepted() -> None:
    # The direction that must not become a refusal: Constant enumerates itself,
    # 'ARCHIVED' is in none of the three, so no row the shape builds is even
    # inside the constraint's condition.
    Shape(
        _companies(50),
        Table(Review, rows=2000, company=FanOut(Zipf()), status=Constant("ARCHIVED")),
    )


def test_a_set_condition_is_refused_on_the_share_of_any_one_member() -> None:
    # A draw that lands inside the set only sometimes is still refused, for the
    # reason the equality form is: a rule about a group cannot be kept by a
    # draw made per row. The quoted arithmetic totals every member of the set,
    # because any of them puts the row inside the condition.
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(50),
            Table(
                Review,
                rows=2000,
                company=FanOut(Zipf()),
                status=Skew({"DRAFT": 0.1, "IN_REVIEW": 0.2, "ARCHIVED": 0.7}),
            ),
        )

    assert "asks for 600 of them" in str(raised.value)


def test_a_per_parent_whose_special_value_is_inside_the_set_is_accepted() -> None:
    # The remedy the message names, spelled against a set condition: one row of
    # each group inside the set, the rest outside it.
    Shape(
        _companies(50),
        Table(
            Review,
            rows=2000,
            company=FanOut(Zipf()),
            status=PerParent("company", last="DRAFT", rest="ARCHIVED"),
        ),
    )


def test_a_per_parent_whose_rest_is_also_inside_the_set_is_refused() -> None:
    # The trap the set form adds: `last` is inside the condition and so is
    # `rest`, so every row of every group matches and the constraint is broken
    # by the declaration that fixes the equality form.
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(50),
            Table(
                Review,
                rows=2000,
                company=FanOut(Zipf()),
                status=PerParent("company", last="DRAFT", rest="IN_REVIEW"),
            ),
        )

    assert "status in ('DRAFT', 'IN_REVIEW', 'APPROVED')" in str(raised.value)


def test_an_or_tree_of_equalities_is_read_like_the_set_it_is() -> None:
    # The third spelling of one rule, and the one the consumer actually wrote.
    # Reading __in and not this made the arithmetic depend on which of two
    # equivalent spellings an ORM offered no reason to choose between -- so the
    # fix for the set form was aimed one return statement too narrowly, which
    # the same consumer said before this test existed.
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(50),
            Table(
                Submission,
                rows=200,
                company=FanOut(Zipf()),
                status=Skew({"DRAFT": 0.5, "COMPLETED": 0.5}),
            ),
        )

    message = str(raised.value)
    assert "status in ('DRAFT', 'IN_REVIEW', 'APPROVED')" in message
    assert "at most 50 rows" in message
    assert "asks for 100 of them" in message


def test_an_or_tree_that_is_nested_and_repeats_a_value_is_read_as_one_set() -> None:
    # Django hands `Q(Q(a) | Q(b))` over as a single child that is itself a Q,
    # a different object graph from the same expression written bare -- so
    # reading both is recursion rather than a second case. And the set is a set:
    # 'OPEN' named twice is one member, or the share below would be totalled
    # twice and quote a number the declaration never asked for.
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(50),
            Table(
                Approval,
                rows=2000,
                company=FanOut(Zipf()),
                status=Skew({"OPEN": 0.25, "ARCHIVED": 0.75}),
            ),
        )

    message = str(raised.value)
    assert "status in ('OPEN', 'HELD')" in message
    assert "asks for 500 of them" in message


def test_a_set_condition_written_as_a_set_is_ordered_before_it_is_quoted() -> None:
    # A set literal reaches the message too, and a message that reordered
    # itself between runs would be one no test could assert on and no reader
    # could compare across two failures.
    with pytest.raises(InvalidShape) as raised:
        Shape(_companies(50), Table(Escalation, rows=2000, company=FanOut(Zipf())))

    assert "status in ('ACKNOWLEDGED', 'RAISED')" in str(raised.value)


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


def test_two_fan_outs_under_unique_together_are_refused_naming_the_tuple() -> None:
    # The same through table spelled the older way. unique_together writes no
    # entry in _meta.constraints, so a check reading only that list let this
    # through and the load died inside COPY -- at line 15 of the edge table for
    # seed 0, measured on PostgreSQL against 0.24.0.
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(20),
            Table(Person, rows=20, name=Constant("p")),
            Table(Engagement, rows=200, company=FanOut(Zipf()), person=FanOut(Zipf())),
        )

    message = str(raised.value)
    assert message.startswith("unique_together ('company', 'person') needs every")
    assert "Engagement.company, Engagement.person are fan-outs" in message


def test_unique_together_is_counted_by_the_pigeonhole_too() -> None:
    # Twenty companies and five people leave a hundred pairs for two hundred
    # rows, and that is the first answer, before the two fan-outs are asked
    # about. Both columns are fan-outs because a Paired partner has no capacity
    # this check can count.
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(20),
            Table(Person, rows=5, name=Constant("p")),
            Table(Engagement, rows=200, company=FanOut(Zipf()), person=FanOut(Zipf())),
        )

    assert str(raised.value).startswith(
        "unique_together ('company', 'person') needs 200 distinct (company, person) "
        "combinations, one per row of Engagement, and this shape can produce 100."
    )


def test_unique_together_takes_the_remedy_its_refusal_names() -> None:
    # Paired is what the message points at, and it has to be accepted under
    # this spelling as it is under a UniqueConstraint, or the refusal would name
    # a way out that is itself refused.
    Shape(
        _companies(20),
        Table(Person, rows=20, name=Constant("p")),
        Table(Engagement, rows=200, company=FanOut(Zipf()), person=Paired("company", Zipf())),
    )


def test_a_tree_beside_a_drawn_column_under_one_uniqueness_is_refused() -> None:
    # A file system's rule: names unique among siblings. The tree gives a row
    # several children whenever its sizes say so, and a name drawn per row
    # cannot see which siblings it has, so two of them sharing one is a matter
    # of the seed. A tree has no capacity this module can count, so the
    # pigeonhole never answers first and this is the only check that can.
    with pytest.raises(InvalidShape, match="one_name_per_folder") as raised:
        Shape(
            Table(
                Folder,
                rows=200,
                parent=Tree(roots=0.05, sizes=Zipf()),
                name=Skew({f"n{i}": 1 for i in range(50)}),
                code=Constant("c"),
            )
        )

    message = str(raised.value)
    assert "Folder.parent is a Tree beside Folder.name=" in message
    # The remedies it names have to be ones a tree accepts: nothing is derived
    # per family of a tree, so the group-scoped derivation the fan-out refusal
    # points at would itself be refused.
    assert "scope='group'" not in message
    assert "Distinct" in message


def test_a_tree_beside_a_column_distinct_in_every_row_is_accepted() -> None:
    # A pair is distinct as soon as either half is, so a Sequential name keeps
    # the constraint on its own, whatever families the tree makes.
    Shape(
        Table(
            Folder,
            rows=200,
            parent=Tree(roots=0.05, sizes=Zipf()),
            name=Sequential(1, 1),
            code=Constant("c"),
        )
    )


def test_a_tree_beside_a_column_that_does_not_move_is_refused() -> None:
    # A zero step writes one value in every row, so the pair is not distinct
    # through the name: the protocol answers for the parameters and not the
    # class. Without the is_distinct_per_row() conjunct, this is accepted.
    with pytest.raises(InvalidShape, match="one_name_per_folder"):
        Shape(
            Table(
                Folder,
                rows=200,
                parent=Tree(roots=0.05, sizes=Zipf()),
                name=Sequential(0, 0),
                code=Constant("c"),
            )
        )


def test_a_tree_beside_a_derivation_is_left_to_the_other_two_nets() -> None:
    # The exemption every arrangement check carries: a derivation reads
    # something other than its own row index, and whether its compute= keeps
    # the siblings apart is not readable here.
    Shape(
        Table(
            Folder,
            rows=200,
            parent=Tree(roots=0.05, sizes=Zipf()),
            name=Derived("code", compute=lambda code: f"{code}-x"),
            code=Skew({"a": 1, "b": 1}),
        )
    )


def test_a_tree_alone_under_a_uniqueness_is_refused_as_a_unique_column_is() -> None:
    # unique=True is refused on the field, and a UniqueConstraint over the one
    # column says the same thing without the field knowing. A tree of a skew gives
    # some parents several children, which one link per row cannot hold.
    with pytest.raises(
        InvalidShape, match="one_link_after_each permits one row per Chain"
    ) as raised:
        Shape(Table(Chain, rows=50, previous=Tree(roots=0.05, sizes=Zipf())))

    assert "previous=Tree(roots=0.05, sizes=Constant(1))" in str(raised.value)
    assert "A chain of one-to-one links is not a tree" not in str(raised.value)


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


def test_one_partition_with_no_group_of_two_is_enough_for_both_fan_outs() -> None:
    # Both flat, and neither can repeat a parent key: twenty rows over twenty
    # companies is one membership each, so no two rows share a company and the
    # pair is distinct on that half alone.
    Shape(
        _companies(20),
        Table(Person, rows=20, name=Constant("p")),
        Table(
            Membership,
            rows=20,
            company=FanOut(Constant(1)),
            person=FanOut(Constant(1)),
            role=Constant("member"),
        ),
    )


def test_the_second_fan_out_does_not_have_to_be_flat_as_well() -> None:
    # The asymmetric case, and the reason the exemption asks for one fan-out
    # rather than both. Five people cannot hold twenty rows one apiece, so the
    # person fan-out could never satisfy the proof at these numbers -- and it
    # does not have to. Twenty companies partitioned flat already make every
    # row's company unique, and this loads twenty times out of twenty.
    Shape(
        _companies(20),
        Table(Person, rows=5, name=Constant("p")),
        Table(
            Membership,
            rows=20,
            company=FanOut(Constant(1)),
            person=FanOut(Zipf()),
            role=Constant("member"),
        ),
    )


def test_two_fan_outs_one_row_past_the_boundary_are_refused_again() -> None:
    # The same boundary as the single-fan-out case. Twenty-one rows over twenty
    # companies gives some company two, and those two rows pick their people
    # independently. Twenty companies and twenty people leave four hundred
    # pairs, so the pigeonhole cannot answer here -- which is what makes this a
    # test of the bound rather than of the arithmetic.
    with pytest.raises(InvalidShape, match="one_membership_per_company_person") as raised:
        Shape(
            _companies(20),
            Table(Person, rows=20, name=Constant("p")),
            Table(
                Membership,
                rows=21,
                company=FanOut(Constant(1)),
                person=FanOut(Zipf()),
                role=Constant("member"),
            ),
        )

    assert "are fan-outs" in str(raised.value)
    assert "can produce" not in str(raised.value)


def test_a_childless_share_takes_the_proof_away_from_two_fan_outs_too() -> None:
    # Flat weights and a childless share is not a flat partition: the childless
    # parents are weighed at zero and their rows go to the others, so some
    # company gets two after all. Measured at twelve loads out of twenty, which
    # is the lottery this refuses rather than a shape that works.
    with pytest.raises(InvalidShape, match="one_membership_per_company_person"):
        Shape(
            _companies(20),
            Table(Person, rows=20, name=Constant("p")),
            Table(
                Membership,
                rows=20,
                company=FanOut(Constant(1), childless=0.1),
                person=FanOut(Zipf()),
                role=Constant("member"),
            ),
        )


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


def test_a_narrowed_fan_out_is_weighed_on_the_parents_it_named() -> None:
    # The capacity of a fan-out column is how many parents it can point at, and
    # parents= makes that the named count rather than the parent's row count.
    # Reading the table's rows here would overstate the room by the ratio of the
    # two -- fifty companies declared, one pinned -- and an overstated capacity
    # does not refuse a shape that cannot fit.
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(50),
            Table(
                Seat,
                rows=20,
                company=FanOut(Constant(1), parents=[1]),
                label=Skew({"a": 1, "b": 1}),
            ),
        )

    message = str(raised.value)
    assert "needs 20 distinct (company, label) combinations" in message
    assert "can produce 2" in message


def test_a_narrowed_fan_out_cannot_collide_on_the_parents_it_named() -> None:
    # The same substitution on the exemption side: twenty rows over twenty named
    # parents gives no parent two rows, whatever the parent table holds.
    Shape(
        _companies(50),
        Table(
            Membership,
            rows=20,
            company=FanOut(Constant(1), parents=list(range(1, 21))),
            person=FanOut(Zipf()),
            role=Constant("r"),
        ),
    )


# What a column of a constraint holds, read the way the load fills it. The models below
# are declared here because a table for each is made and dropped by the test that loads
# it; both keywords arrived in Django 5.0, and the tests reading them skip below that.
_NEEDS_DJANGO_5 = pytest.mark.skipif(
    django.VERSION < (5, 0), reason="db_default and nulls_distinct arrived in Django 5.0"
)
_TOGETHER: dict[str, bool] = {"nulls_distinct": False} if django.VERSION >= (5, 0) else {}


def _db_default(value: object) -> dict[str, object]:
    return {"db_default": value} if django.VERSION >= (5, 0) else {}


def _self_key() -> models.ForeignKey[Any, Any]:
    return models.ForeignKey("self", null=True, on_delete=models.CASCADE, related_name="+")


def _key(to: type[models.Model]) -> models.ForeignKey[Any, Any]:
    return models.ForeignKey(to, null=True, on_delete=models.CASCADE, related_name="+")


with isolate_apps("tests.testapp"):

    class DefaultedFolder(models.Model):
        parent = _self_key()
        name = models.CharField(max_length=50, null=True, **_db_default("same"))

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["parent", "name"], name="one_defaulted_folder"),
            ]

    class DefaultedSeat(models.Model):
        company = _key(Company)
        label = models.CharField(max_length=50, null=True, **_db_default("same"))

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["company", "label"], name="one_defaulted_seat"),
            ]

    class DefaultedPair(models.Model):
        first = models.CharField(max_length=50, null=True, **_db_default("same"))
        second = models.IntegerField(null=True, **_db_default(1))

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["first", "second"], name="one_defaulted_pair"),
            ]

    class ValueDefaultedSeat(models.Model):
        """The Value a caller can write, which is also what Django 5.0 makes of a literal."""

        company = _key(Company)
        label = models.CharField(max_length=50, null=True, **_db_default(models.Value("same")))

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["company", "label"], name="one_value_seat"),
            ]

    class TokenFolder(models.Model):
        """A db_default that is an expression, which this one makes new for every row."""

        parent = _self_key()
        token = models.UUIDField(
            null=True,
            **_db_default(models.Func(function="gen_random_uuid", output_field=models.UUIDField())),
        )

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["parent", "token"], name="one_token_folder"),
            ]

    class TokenCondition(models.Model):
        company = _key(Company)
        token = models.UUIDField(
            null=True,
            **_db_default(models.Func(function="gen_random_uuid", output_field=models.UUIDField())),
        )

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company"], condition=models.Q(token=None), name="one_token_condition"
                ),
            ]

    class NoneDefaultedFolder(models.Model):
        parent = _self_key()
        name = models.CharField(max_length=50, null=True, **_db_default(None))

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["parent", "name"], name="one_none_folder"),
            ]

    class DefaultedKey(models.Model):
        """A key this package fills itself, so its db_default is never what the row holds."""

        id = models.IntegerField(primary_key=True, **_db_default(1))
        company = _key(Company)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["id", "company"], name="one_defaulted_key"),
            ]

    class AttnameFolder(models.Model):
        parent = _self_key()
        name = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["parent_id", "name"], name="one_attname_folder"),
            ]

    class AttnameTogether(models.Model):
        parent = _self_key()
        name = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            unique_together = [("parent_id", "name")]

    class AttnameGroup(models.Model):
        company = _key(Company)
        status = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company_id"],
                    condition=models.Q(status="on"),
                    name="one_attname_group",
                ),
            ]

    class TiedSeat(models.Model):
        company = _key(Company)
        label = models.CharField(max_length=50, null=True)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company", "label"], name="one_tied_seat", **_TOGETHER
                ),
            ]

    class TiedColumn(models.Model):
        company = _key(Company)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["company"], name="one_tied_column", **_TOGETHER),
            ]

    class OneColumn(models.Model):
        company = _key(Company)

        class Meta:
            app_label = "testapp"
            constraints = [models.UniqueConstraint(fields=["company"], name="one_per_column")]

    class OneColumnTogether(models.Model):
        company = _key(Company)

        class Meta:
            app_label = "testapp"
            unique_together = [("company",)]

    class TiedEdge(models.Model):
        company = _key(Company)
        person = _key(Person)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company", "person"], name="one_tied_edge", **_TOGETHER
                ),
            ]

    class CodedRule(models.Model):
        company = _key(Company)
        code = models.IntegerField()
        status = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company", "code"],
                    condition=models.Q(status="on"),
                    name="one_coded_rule",
                ),
            ]

    class TiedCodedRule(models.Model):
        company = _key(Company)
        code = models.IntegerField()
        status = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company", "code"],
                    condition=models.Q(status="on"),
                    name="one_tied_coded_rule",
                    **_TOGETHER,
                ),
            ]

    class LabelledRule(models.Model):
        company = _key(Company)
        label = models.CharField(max_length=50, null=True)
        status = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company", "label"],
                    condition=models.Q(status="on"),
                    name="one_labelled_rule",
                ),
            ]

    class Triple(models.Model):
        company = _key(Company)
        person = _key(Person)
        code = models.IntegerField(null=True)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["company", "person", "code"], name="one_triple"),
            ]

    class TiedTriple(models.Model):
        company = _key(Company)
        person = _key(Person)
        code = models.IntegerField()

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company", "person", "code"], name="one_tied_triple", **_TOGETHER
                ),
            ]

    class TreeTriple(models.Model):
        parent = _self_key()
        company = _key(Company)
        person = _key(Person)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["parent", "company", "person"], name="one_tree_triple"
                ),
            ]

    class TiedTreeTriple(models.Model):
        parent = _self_key()
        company = _key(Company)
        person = _key(Person)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["parent", "company", "person"], name="one_tied_tree_triple", **_TOGETHER
                ),
            ]

    class TiedChain(models.Model):
        previous = _self_key()

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["previous"], name="one_tied_chain", **_TOGETHER),
            ]

    class TiedNamed(models.Model):
        parent = _self_key()
        name = models.CharField(max_length=50, null=True)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["parent", "name"], name="one_tied_named", **_TOGETHER
                ),
            ]

    class TiedAnswer(models.Model):
        parent = _self_key()
        status = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["parent"],
                    condition=models.Q(status="accepted"),
                    name="one_tied_answer",
                    **_TOGETHER,
                ),
            ]

    class NullRule(models.Model):
        company = _key(Company)
        status = models.CharField(max_length=50, null=True)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company"], condition=models.Q(status=None), name="one_null_rule"
                ),
            ]

    class NullRuleOverATree(models.Model):
        parent = _self_key()
        status = models.CharField(max_length=50, null=True)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["parent"], condition=models.Q(status=None), name="one_null_tree_rule"
                ),
            ]

    class DefaultOnRule(models.Model):
        company = _key(Company)
        status = models.CharField(max_length=50, null=True, **_db_default("on"))

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company"], condition=models.Q(status="on"), name="one_default_on_rule"
                ),
            ]

    class DefaultOffRule(models.Model):
        company = _key(Company)
        status = models.CharField(max_length=50, null=True, **_db_default("off"))

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company"], condition=models.Q(status="on"), name="one_default_off_rule"
                ),
            ]

    class StampedFolder(models.Model):
        """Now() as a db_default, which every row of one COPY reads alike."""

        parent = _self_key()
        at = models.DateTimeField(null=True, **_db_default(Now()))

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["parent", "at"], name="one_stamped_folder"),
            ]

    class StampedSeat(models.Model):
        company = _key(Company)
        at = models.DateTimeField(null=True, **_db_default(Now()))

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["company", "at"], name="one_stamped_seat"),
            ]

    class StampedOnly(models.Model):
        at = models.DateTimeField(null=True, **_db_default(Now()))

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["at"], name="one_stamped_only"),
            ]

    class StampedUnique(models.Model):
        at = models.DateTimeField(null=True, unique=True, **_db_default(Now()))

        class Meta:
            app_label = "testapp"

    class CodedUnique(models.Model):
        code = models.CharField(max_length=8, unique=True, **_db_default("on"))

        class Meta:
            app_label = "testapp"

    class DatedUnique(models.Model):
        day = models.DateField(unique=True, **_db_default(datetime.date(2020, 1, 1)))

        class Meta:
            app_label = "testapp"

    class UuidCodedUnique(models.Model):
        token = models.UUIDField(unique=True, **_db_default(uuid.UUID(int=1)))

        class Meta:
            app_label = "testapp"

    class TokenedUnique(models.Model):
        token = models.UUIDField(
            null=True,
            unique=True,
            **_db_default(models.Func(function="gen_random_uuid", output_field=models.UUIDField())),
        )

        class Meta:
            app_label = "testapp"

    class CodedRepeating(models.Model):
        code = models.CharField(max_length=8, **_db_default("on"))

        class Meta:
            app_label = "testapp"

    class NullableUnique(models.Model):
        code = models.CharField(max_length=8, null=True, unique=True)

        class Meta:
            app_label = "testapp"

    class JsonRule(models.Model):
        """A JSON column left out under a dict db_default, beside a condition on another dict."""

        company = _key(Company)
        meta = models.JSONField(**_db_default(models.Value({"a": 1}, models.JSONField())))

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company"], condition=models.Q(meta={"b": 2}), name="one_json_rule"
                ),
            ]

    class JsonMetRule(models.Model):
        company = _key(Company)
        meta = models.JSONField(**_db_default(models.Value({"a": 1}, models.JSONField())))

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company"], condition=models.Q(meta={"a": 1}), name="one_json_met_rule"
                ),
            ]

    class UnstampedRule(models.Model):
        company = _key(Company)
        at = models.DateTimeField(null=True, **_db_default(Now()))

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company"], condition=models.Q(at=None), name="one_unstamped_rule"
                ),
            ]

    class OnRule(models.Model):
        """One row inside the condition per company, with the status drawn per row."""

        company = _key(Company)
        status = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company"], condition=models.Q(status="on"), name="one_on_rule"
                ),
            ]

    class TiedOnRule(models.Model):
        company = _key(Company)
        status = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company"],
                    condition=models.Q(status="on"),
                    name="one_tied_on_rule",
                    **_TOGETHER,
                ),
            ]

    class OnRuleBetweenTwo(models.Model):
        company = _key(Company)
        person = _key(Person)
        status = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company", "person"],
                    condition=models.Q(status="on"),
                    name="one_on_rule_between_two",
                ),
            ]

    class BranchOnRule(models.Model):
        parent = _self_key()
        company = _key(Company)
        status = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["parent", "company"],
                    condition=models.Q(status="on"),
                    name="one_on_rule_on_a_branch",
                ),
            ]

    class UnownedRule(models.Model):
        company = _key(Company)
        owner = _key(Person)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company"], condition=models.Q(owner_id=None), name="one_unowned_rule"
                ),
            ]

    class KeyRule(models.Model):
        company = _key(Company)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(
                    fields=["company"], condition=models.Q(pk=None), name="one_key_rule"
                ),
            ]

    class GeneratedSeat(models.Model):
        """A column the database computes per row, which the load also leaves out."""

        company = _key(Company)
        number = models.IntegerField()
        if django.VERSION >= (5, 0):
            doubled = models.GeneratedField(
                expression=models.F("number") * 2,
                output_field=models.IntegerField(),
                db_persist=True,
                null=True,
            )

        class Meta:
            app_label = "testapp"
            constraints = (
                [
                    models.UniqueConstraint(
                        fields=["company", "doubled"], name="one_generated_seat", **_TOGETHER
                    ),
                ]
                if django.VERSION >= (5, 0)
                else []
            )


def _people(rows: int = 50) -> Table:
    return Table(Person, rows=rows, name=Constant("ada"))


def _one_root_chain(rows: int = 200) -> Tree:
    """A root share that comes to one row, so every other row has a parent of its own."""
    return Tree(roots=1 / (rows + 1), sizes=Constant(1))


def _assert_builds(owned: tuple[type[models.Model], ...], *tables: Table) -> None:
    """Accepted at declaration, and it loads at several seeds on a real unique index."""
    with connection.schema_editor() as editor:
        for model in owned:
            editor.create_model(model)
    try:
        for seed in (0, 1, 7, 42, 1000):
            build_shape(Shape(*tables, seed=seed))
            names = ", ".join(
                connection.ops.quote_name(table.model._meta.db_table) for table in tables
            )
            with connection.cursor() as cursor:
                cursor.execute(f"TRUNCATE {names} CASCADE")
    finally:
        with connection.schema_editor() as editor:
            for model in owned:
                editor.delete_model(model)


_ON_POSTGRES = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="the uniqueness is enforced by a PostgreSQL index under COPY",
)


@_NEEDS_DJANGO_5
@pytest.mark.parametrize(
    ("tables", "constraint", "holds"),
    [
        pytest.param(
            lambda: [Table(DefaultedFolder, rows=200, parent=Tree(roots=0.05, sizes=Zipf()))],
            "one_defaulted_folder",
            "'same'",
            id="beside_a_tree",
        ),
        pytest.param(
            lambda: [_companies(), Table(DefaultedSeat, rows=20, company=FanOut(Zipf()))],
            "one_defaulted_seat",
            "'same'",
            id="beside_a_fan_out",
        ),
        pytest.param(
            lambda: [_companies(), Table(ValueDefaultedSeat, rows=20, company=FanOut(Zipf()))],
            "one_value_seat",
            "'same'",
            id="written_as_a_value",
        ),
        # Measured at 0 of 20 on each, past the checks: Now() is STATEMENT_TIMESTAMP() on
        # PostgreSQL, and one table's rows arrive in one COPY statement.
        pytest.param(
            lambda: [Table(StampedFolder, rows=200, parent=Tree(roots=0.05, sizes=Zipf()))],
            "one_stamped_folder",
            "Now()",
            id="now_beside_a_tree",
        ),
        pytest.param(
            lambda: [_companies(), Table(StampedSeat, rows=20, company=FanOut(Zipf()))],
            "one_stamped_seat",
            "Now()",
            id="now_beside_a_fan_out",
        ),
    ],
)
def test_a_db_default_is_one_value_in_every_row(tables: Any, constraint: str, holds: str) -> None:
    # Measured at 0 of 20 on both. The column is left out of COPY, so the database
    # writes the default in every row: it was read as a NULL, which keeps rows apart.
    with pytest.raises(InvalidShape) as raised:
        Shape(*tables())

    message = str(raised.value)
    assert constraint in message
    assert "db_default" in message
    assert holds in message


@_NEEDS_DJANGO_5
def test_db_defaults_alone_count_as_one_combination() -> None:
    with pytest.raises(InvalidShape) as raised:
        Shape(Table(DefaultedPair, rows=5))

    assert "one_defaulted_pair" in str(raised.value)
    assert "can produce 1" in str(raised.value)


@_NEEDS_DJANGO_5
def test_a_db_default_counts_toward_the_room_a_fan_out_has() -> None:
    with pytest.raises(InvalidShape) as raised:
        Shape(_companies(5), Table(DefaultedSeat, rows=6, company=FanOut(Zipf())))

    assert "can produce 5" in str(raised.value)


@pytest.mark.parametrize(
    ("tables", "constraint"),
    [
        pytest.param(
            lambda: [
                Table(
                    AttnameFolder,
                    rows=200,
                    parent=Tree(roots=0.05, sizes=Zipf()),
                    name=Skew({"a": 1, "b": 1}),
                )
            ],
            "one_attname_folder",
            id="constraint",
        ),
        pytest.param(
            lambda: [
                Table(
                    AttnameTogether,
                    rows=200,
                    parent=Tree(roots=0.05, sizes=Zipf()),
                    name=Skew({"a": 1, "b": 1}),
                )
            ],
            "unique_together ('parent_id', 'name')",
            id="unique_together",
        ),
        pytest.param(
            lambda: [
                _companies(),
                Table(
                    AttnameGroup,
                    rows=200,
                    company=FanOut(Zipf()),
                    status=Skew({"on": 1, "off": 1}),
                ),
            ],
            "one_attname_group",
            id="conditional",
        ),
    ],
)
def test_a_column_named_by_its_attname_is_the_column(tables: Any, constraint: str) -> None:
    # Django accepts either spelling in a constraint and builds one index, and each of
    # these loaded 0 of 20 while table.fields, keyed on names, missed the attname.
    with pytest.raises(InvalidShape) as raised:
        Shape(*tables())

    assert constraint in str(raised.value)


@_NEEDS_DJANGO_5
@pytest.mark.parametrize(
    ("tables", "constraint"),
    [
        pytest.param(
            lambda: [_companies(), Table(TiedSeat, rows=20, company=FanOut(Zipf()))],
            "one_tied_seat",
            id="beside_a_left_out_column",
        ),
        pytest.param(
            lambda: [_companies(), Table(TiedColumn, rows=20, company=FanOut(Zipf()))],
            "one_tied_column",
            id="alone",
        ),
        pytest.param(
            lambda: [
                _companies(),
                Table(TiedSeat, rows=20, company=FanOut(Zipf(), max_children=1, null=0.5)),
            ],
            "one_tied_seat",
            id="capped_with_a_null_share_beside_a_left_out_column",
        ),
        pytest.param(
            lambda: [
                _companies(),
                Table(
                    TiedSeat,
                    rows=20,
                    company=FanOut(Zipf(), max_children=1, null=0.5),
                    label=Skew({"a": 1, "b": 1}),
                ),
            ],
            "one_tied_seat",
            id="capped_with_a_null_share_beside_a_draw",
        ),
        pytest.param(
            lambda: [
                _companies(),
                Table(TiedColumn, rows=20, company=FanOut(Zipf(), max_children=1, null=0.5)),
            ],
            "one_tied_column",
            id="capped_with_a_null_share_alone",
        ),
        pytest.param(
            lambda: [
                _companies(),
                _people(),
                Table(
                    TiedEdge,
                    rows=20,
                    company=FanOut(Zipf(), max_children=1, null=0.5),
                    person=FanOut(Zipf()),
                ),
            ],
            "one_tied_edge",
            id="capped_with_a_null_share_beside_a_fan_out",
        ),
        pytest.param(
            lambda: [Table(TiedNamed, rows=200, parent=Tree(roots=0.05, sizes=Constant(1)))],
            "one_tied_named",
            id="flat_tree_of_many_roots_beside_a_left_out_column",
        ),
        pytest.param(
            lambda: [Table(TiedNamed, rows=200, parent=Tree(roots=0.05, sizes=Zipf()))],
            "one_tied_named",
            id="tree_beside_a_left_out_column",
        ),
    ],
)
def test_nulls_counted_together_are_one_value_where_the_checks_read_them(
    tables: Any, constraint: str
) -> None:
    # Every one of these rested on "each NULL is its own group" and died in COPY.
    with pytest.raises(InvalidShape) as raised:
        Shape(*tables())

    assert constraint in str(raised.value)


@_NEEDS_DJANGO_5
def test_a_null_share_counted_together_is_one_more_parent() -> None:
    # Unbounded while each NULL is its own group, and one value here: five companies,
    # the NULL and two labels are twelve combinations for five hundred rows.
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(5),
            Table(
                TiedSeat,
                rows=500,
                company=FanOut(Zipf(), null=0.1),
                label=Skew({"a": 1, "b": 1}),
            ),
        )

    assert "can produce 12" in str(raised.value)


@pytest.mark.parametrize(
    ("model", "named"),
    [(OneColumn, "one_per_column"), (OneColumnTogether, "unique_together ('company',)")],
)
def test_a_fan_out_alone_under_a_uniqueness_is_the_unique_column_it_amounts_to(
    model: type[models.Model], named: str
) -> None:
    # Measured at 0 of 20. A unique=True is read off the field and refused; this spelling
    # writes the same index and no check read it.
    with pytest.raises(InvalidShape) as raised:
        Shape(_companies(), Table(model, rows=20, company=FanOut(Zipf())))

    message = str(raised.value)
    assert named in message
    assert "max_children=1" in message
    assert "nulls_distinct" not in message


@_NEEDS_DJANGO_5
def test_a_fan_out_alone_says_when_the_null_share_is_what_collides() -> None:
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(),
            Table(TiedColumn, rows=20, company=FanOut(Zipf(), max_children=1, null=0.5)),
        )

    assert "nulls_distinct=False" in str(raised.value)


@_ON_POSTGRES
@_NEEDS_DJANGO_5
@pytest.mark.django_db(transaction=True)
def test_a_fan_out_alone_offers_what_loads_once_the_null_share_is_gone() -> None:
    # Both offers, applied with the null share kept, are refused again: its rows are one
    # group, and they are enough rows to collide.
    for kept in (
        FanOut(Zipf(), max_children=1, null=0.3),
        FanOut(Constant(1), null=0.3),
    ):
        with pytest.raises(InvalidShape):
            Shape(_companies(), Table(TiedColumn, rows=20, company=kept))
    with pytest.raises(InvalidShape) as raised:
        Shape(_companies(), Table(TiedColumn, rows=20, company=FanOut(Zipf(), null=0.3)))

    message = str(raised.value)
    assert "max_children=1 and no null share" in message
    assert "FanOut(Constant(1)) with no childless share and no null share" in message
    # Each offer as written, with the null share dropped, at several seeds.
    for offered in (FanOut(Zipf(), max_children=1), FanOut(Constant(1))):
        _assert_builds((TiedColumn,), _companies(), Table(TiedColumn, rows=20, company=offered))


def test_a_fan_out_alone_names_no_null_share_where_there_is_none() -> None:
    with pytest.raises(InvalidShape) as raised:
        Shape(_companies(), Table(TiedColumn, rows=20, company=FanOut(Zipf())))

    assert "no null share" not in str(raised.value)
    assert "max_children=1, which caps every parent" in str(raised.value)


def test_a_null_share_keeps_the_cap_while_each_null_is_its_own_group() -> None:
    Shape(_companies(), Table(OneColumn, rows=20, company=FanOut(Zipf(), max_children=1, null=0.5)))
    Shape(
        _companies(),
        Table(
            Seat,
            rows=20,
            company=FanOut(Zipf(), max_children=1, null=0.5),
            label=Skew({"a": 1, "b": 1}),
        ),
    )


@_NEEDS_DJANGO_5
def test_a_condition_nothing_can_read_is_left_alone() -> None:
    # Q(token=None) over a column the database fills with an expression: nothing here
    # can say what it holds, so this is accepted, and a load of it is in
    # test_each_exemption_is_accepted_and_loads.
    Shape(_companies(), Table(TokenCondition, rows=200, company=FanOut(Zipf())))


@_NEEDS_DJANGO_5
def test_a_dict_db_default_is_compared_to_a_condition_not_hashed() -> None:
    # Constant.shares() keys a dict by the dict, so reading a left-out JSONField through it
    # raised a TypeError at Shape(). A condition the column never meets leaves the
    # constraint nothing to count, so it is accepted; one it meets is a refusal.
    Shape(_companies(), Table(JsonRule, rows=200, company=FanOut(Zipf())))

    with pytest.raises(InvalidShape) as raised:
        Shape(_companies(), Table(JsonMetRule, rows=200, company=FanOut(Zipf())))

    assert "one_json_met_rule" in str(raised.value)
    assert "asks for 200 of them" in str(raised.value)


def test_a_condition_may_name_a_left_out_key_by_its_attname() -> None:
    # Q(owner_id=None) over a key the shape leaves out puts every row inside the
    # condition, measured at 0 of 20. No field is called owner_id, so without the
    # attname's name this read as a column nothing can read and was accepted.
    with pytest.raises(InvalidShape) as raised:
        Shape(_companies(), Table(UnownedRule, rows=200, company=FanOut(Zipf())))

    assert "one_unowned_rule" in str(raised.value)
    assert "NULL in every row" in str(raised.value)


@_NEEDS_DJANGO_5
@pytest.mark.parametrize(
    ("build", "column", "held"),
    [
        pytest.param(lambda: Table(CodedUnique, rows=20), "CodedUnique.code", "'on'", id="literal"),
        pytest.param(lambda: Table(StampedUnique, rows=20), "StampedUnique.at", "Now()", id="now"),
    ],
)
def test_a_unique_column_left_to_a_db_default_of_one_value_is_refused(
    build: Any, column: str, held: str
) -> None:
    # Each loaded 0 of 20: unique=True writes no constraint, so no check read the
    # column, and the second row of the COPY repeated the first row's value.
    with pytest.raises(InvalidShape) as raised:
        Shape(build())

    message = str(raised.value)
    assert column in message
    assert "is unique" in message
    assert f"its db_default {held} in every row" in message
    assert "20 rows" in message


@_ON_POSTGRES
@_NEEDS_DJANGO_5
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("model", "column", "offered"),
    [
        pytest.param(
            StampedUnique,
            "at",
            Sequential(_START, datetime.timedelta(seconds=1)),
            id="datetime",
        ),
        pytest.param(CodedUnique, "code", Sequential(1, 1), id="char"),
        pytest.param(
            DatedUnique,
            "day",
            Sequential(datetime.date(2020, 1, 1), datetime.timedelta(days=1)),
            id="date",
        ),
    ],
)
def test_the_distribution_a_unique_left_out_column_is_offered_loads(
    model: type[models.Model], column: str, offered: Sequential
) -> None:
    # The old offer, a db_default the database makes new in every row, has no true
    # instance for a DateTimeField: Func(function="clock_timestamp") loaded 0 of 3.
    with pytest.raises(InvalidShape) as raised:
        Shape(Table(model, rows=20))

    message = str(raised.value)
    assert f"{column}={offered!r}" in message
    assert "db_default the database makes new" not in message
    # What the message names, built as written, at several seeds.
    _assert_builds((model,), Table(model, rows=20, **{column: offered}))


@_NEEDS_DJANGO_5
def test_a_unique_uuid_left_out_is_offered_the_db_default_that_is_new_in_every_row() -> None:
    # A UUID has no Distinct distribution to name, and gen_random_uuid() is the one
    # db_default that is new in every row, which test_each_exemption_is_accepted_and_loads
    # builds as TokenedUnique.
    with pytest.raises(InvalidShape) as raised:
        Shape(Table(UuidCodedUnique, rows=20))

    message = str(raised.value)
    assert "gen_random_uuid" in message
    assert "Sequential" not in message


def _fallback_model(suffix: str, field: models.Field[Any, Any]) -> type[models.Model]:
    """A unique column left to one db_default, one table per field type: testapp_fb_<suffix>."""
    with isolate_apps("tests.testapp"):
        meta = type("Meta", (), {"app_label": "testapp"})
        return cast(
            "type[models.Model]",
            type(
                f"fb_{suffix}", (models.Model,), {"__module__": __name__, "v": field, "Meta": meta}
            ),
        )


def _decimal(digits: int, places: int) -> models.Field[Any, Any]:
    return models.DecimalField(
        max_digits=digits, decimal_places=places, unique=True, db_default=decimal.Decimal(1)
    )


# Each loads Sequential(1, 1) twenty times in twenty at two hundred rows. The text and decimal
# cases sit on the bound: three characters, and three digits before the point.
@_ON_POSTGRES
@_NEEDS_DJANGO_5
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("suffix", "field"),
    [
        pytest.param("char3", lambda: models.CharField(max_length=3, unique=True, db_default="a")),
        pytest.param("text", lambda: models.TextField(unique=True, db_default="a")),
        pytest.param("int", lambda: models.IntegerField(unique=True, db_default=1)),
        pytest.param("smallint", lambda: models.SmallIntegerField(unique=True, db_default=1)),
        pytest.param("float", lambda: models.FloatField(unique=True, db_default=1.0)),
        pytest.param("json", lambda: models.JSONField(unique=True, db_default=1)),
        pytest.param(
            "duration",
            lambda: models.DurationField(unique=True, db_default=datetime.timedelta(1)),
        ),
        pytest.param("dec52", lambda: _decimal(5, 2)),
    ],
)
def test_the_fallback_is_offered_where_it_builds(suffix: str, field: Any) -> None:
    model = _fallback_model(suffix, field())
    with pytest.raises(InvalidShape) as raised:
        Shape(Table(model, rows=200))

    assert "v=Sequential(1, 1)" in str(raised.value)
    _assert_builds((model,), Table(model, rows=200, v=Sequential(1, 1)))


# Sequential(1, 1) loads none of twenty in each, at two hundred rows and at two for a boolean:
# a value too long, a numeric overflow, a TypeError or a ValidationError.
@_NEEDS_DJANGO_5
@pytest.mark.parametrize(
    ("suffix", "field", "rows"),
    [
        pytest.param(
            "char2", lambda: models.CharField(max_length=2, unique=True, db_default="a"), 200
        ),
        pytest.param(
            "slug2", lambda: models.SlugField(max_length=2, unique=True, db_default="a"), 200
        ),
        pytest.param("dec32", lambda: _decimal(3, 2), 200),
        pytest.param("dec42", lambda: _decimal(4, 2), 200),
        pytest.param(
            "time", lambda: models.TimeField(unique=True, db_default=datetime.time(1)), 200
        ),
        pytest.param(
            "ip",
            lambda: models.GenericIPAddressField(unique=True, db_default="1.1.1.1"),
            200,
        ),
        pytest.param("binary", lambda: models.BinaryField(unique=True, db_default=b"a"), 200),
        pytest.param("bool", lambda: models.BooleanField(unique=True, db_default=True), 2),
        pytest.param(
            "smallint_full",
            lambda: models.SmallIntegerField(unique=True, db_default=1),
            32768,
        ),
        pytest.param(
            "psmallint_full",
            lambda: models.PositiveSmallIntegerField(unique=True, db_default=1),
            32768,
        ),
    ],
)
def test_the_fallback_is_withheld_where_it_cannot_build(suffix: str, field: Any, rows: int) -> None:
    with pytest.raises(InvalidShape) as raised:
        Shape(Table(_fallback_model(suffix, field()), rows=rows))

    message = str(raised.value)
    assert "Sequential" not in message
    assert "Projection with columns= and sql=" in message


@_NEEDS_DJANGO_5
@pytest.mark.parametrize(
    ("suffix", "field"),
    [
        pytest.param("smallint_last", lambda: models.SmallIntegerField(unique=True, db_default=1)),
        pytest.param(
            "psmallint_last",
            lambda: models.PositiveSmallIntegerField(unique=True, db_default=1),
        ),
    ],
)
def test_the_fallback_is_offered_up_to_the_last_value_a_small_integer_holds(
    suffix: str, field: Any
) -> None:
    # 32767 is the largest value either holds, and Sequential(1, 1) writes 1 to rows.
    with pytest.raises(InvalidShape) as raised:
        Shape(Table(_fallback_model(suffix, field()), rows=32767))

    assert "v=Sequential(1, 1)" in str(raised.value)


@_NEEDS_DJANGO_5
def test_the_tree_left_out_of_a_self_key_that_ties_its_nulls_is_not_a_remedy() -> None:
    # A Tree refusal does not offer this, and here is why: every row would hold the
    # one NULL the constraint allows once. Shape says so before a row is written.
    with pytest.raises(InvalidShape, match="one_tied_link_after_each.*can produce 1"):
        Shape(Table(TiedLink, rows=200))


@_NEEDS_DJANGO_5
def test_now_alone_counts_as_one_combination() -> None:
    with pytest.raises(InvalidShape) as raised:
        Shape(Table(StampedOnly, rows=20))

    assert "one_stamped_only" in str(raised.value)
    assert "can produce 1" in str(raised.value)


@_NEEDS_DJANGO_5
def test_a_cap_of_one_under_a_condition_is_no_proof_where_its_nulls_are_one_group() -> None:
    # Loads 0 of 20: the rows of the null share are one group under nulls_distinct=False,
    # and every one of them is inside the condition. _cannot_collide alone says yes here.
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(),
            Table(
                TiedOnRule,
                rows=20,
                company=FanOut(Zipf(), max_children=1, null=0.5),
                status=Constant("on"),
            ),
        )

    assert "one_tied_on_rule" in str(raised.value)


def test_a_condition_every_left_out_row_lands_inside_is_refused() -> None:
    # Q(status=None) is IS NULL, and a column the shape leaves out holds NULL, so every
    # row is inside the condition. Measured at 0 of 20: the guard this replaces read
    # the same column as saying nothing.
    with pytest.raises(InvalidShape) as raised:
        Shape(_companies(), Table(NullRule, rows=200, company=FanOut(Zipf())))

    assert "one_null_rule" in str(raised.value)
    assert "NULL in every row" in str(raised.value)


def test_a_condition_every_left_out_row_lands_inside_is_refused_over_a_tree() -> None:
    with pytest.raises(InvalidShape) as raised:
        Shape(Table(NullRuleOverATree, rows=200, parent=Tree(roots=0.05, sizes=Zipf())))

    assert "one_null_tree_rule" in str(raised.value)


@_NEEDS_DJANGO_5
def test_a_db_default_inside_the_condition_is_refused() -> None:
    with pytest.raises(InvalidShape) as raised:
        Shape(_companies(), Table(DefaultOnRule, rows=200, company=FanOut(Zipf())))

    assert "one_default_on_rule" in str(raised.value)
    assert "db_default" in str(raised.value)


def test_a_distinct_column_does_not_excuse_one_that_does_not_move() -> None:
    # Sequential is the class and the step is the answer: the is_distinct_per_row half of
    # the proof. Measured at 0 of 20 at a step of zero.
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(),
            Table(
                CodedRule,
                rows=200,
                company=FanOut(Zipf()),
                code=Sequential(1, 0),
                status=Skew({"on": 1, "off": 1}),
            ),
        )

    assert "one_coded_rule" in str(raised.value)


def test_a_tree_of_many_children_beside_two_fan_outs_stays_refused() -> None:
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(5),
            _people(5),
            Table(
                TreeTriple,
                rows=200,
                parent=Tree(roots=0.05, sizes=Zipf()),
                company=FanOut(Zipf()),
                person=FanOut(Zipf()),
            ),
        )

    assert "one_tree_triple" in str(raised.value)


@_NEEDS_DJANGO_5
@pytest.mark.parametrize(
    ("tree", "rows"),
    [
        pytest.param(Tree(roots=0.01, sizes=Constant(1)), 200, id="two_roots_flat"),
        pytest.param(Tree(roots=0.005, sizes=Zipf()), 200, id="one_root_with_siblings"),
    ],
)
def test_one_root_is_not_enough_and_nor_is_a_flat_tree(tree: Tree, rows: int) -> None:
    # The two halves of the proof under nulls_distinct=False. Both measured at 0 of 20.
    with pytest.raises(InvalidShape) as raised:
        Shape(Table(TiedChain, rows=rows, previous=tree))

    message = str(raised.value)
    assert "one_tied_chain" in message
    assert "one root" in message
    assert f"1/{rows + 1}" in message


@_ON_POSTGRES
@_NEEDS_DJANGO_5
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("rows", [11, 13])
def test_the_one_root_a_refusal_offers_is_one_root_at_any_row_count(rows: int) -> None:
    # roots=1/rows is two roots at rows 11 and 13 among others: the share is read through
    # its decimal spelling and repr(1/11) is a hair above the fraction. The offer is
    # 1/(rows + 1), and it is accepted and loads.
    with pytest.raises(InvalidShape) as raised:
        Shape(Table(TiedChain, rows=rows, previous=Tree(roots=0.3, sizes=Constant(1))))

    assert f"roots at most 1/{rows + 1}" in str(raised.value)
    one_root = Tree(roots=1 / (rows + 1), sizes=Constant(1))
    _assert_builds((TiedChain,), Table(TiedChain, rows=rows, previous=one_root))


@_NEEDS_DJANGO_5
def test_a_flat_tree_of_many_roots_counted_together_stays_refused_beside_two_fan_outs() -> None:
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(5),
            _people(5),
            Table(
                TiedTreeTriple,
                rows=200,
                parent=Tree(roots=0.5, sizes=Constant(1)),
                company=FanOut(Zipf()),
                person=FanOut(Zipf()),
            ),
        )

    assert "one_tied_tree_triple" in str(raised.value)


@_NEEDS_DJANGO_5
def test_one_root_is_offered_beside_a_draw_and_in_a_conditional_when_it_would_hold() -> None:
    for message in (
        str(
            pytest.raises(
                InvalidShape,
                Shape,
                Table(
                    TiedNamed,
                    rows=200,
                    parent=Tree(roots=0.05, sizes=Constant(1)),
                    name=Skew({"a": 1, "b": 1}),
                ),
            ).value
        ),
        str(
            pytest.raises(
                InvalidShape,
                Shape,
                Table(
                    TiedAnswer,
                    rows=200,
                    parent=Tree(roots=0.05, sizes=Constant(1)),
                    status=Constant("accepted"),
                ),
            ).value
        ),
    ):
        assert "one root" in message
        assert "1/201" in message


@_ON_POSTGRES
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("owned", "tables"),
    [
        # A proof that keeps the constraint whatever the db_default is.
        pytest.param(
            (DefaultedFolder,),
            lambda: [Table(DefaultedFolder, rows=200, parent=Tree(roots=0.05, sizes=Constant(1)))],
            id="db_default_beside_a_flat_tree",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (DefaultedSeat,),
            lambda: [
                _companies(),
                Table(DefaultedSeat, rows=20, company=FanOut(Zipf(), max_children=1)),
            ],
            id="db_default_beside_a_cap_of_one",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (DefaultedPair,),
            lambda: [Table(DefaultedPair, rows=1)],
            id="db_defaults_on_one_row",
            marks=_NEEDS_DJANGO_5,
        ),
        # What a db_default leaves readable as something other than one value.
        pytest.param(
            (TokenFolder,),
            lambda: [Table(TokenFolder, rows=200, parent=Tree(roots=0.05, sizes=Zipf()))],
            id="expression_db_default_new_in_every_row",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (NoneDefaultedFolder,),
            lambda: [Table(NoneDefaultedFolder, rows=200, parent=Tree(roots=0.05, sizes=Zipf()))],
            id="db_default_of_none",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (DefaultedKey,),
            lambda: [_companies(), Table(DefaultedKey, rows=20, company=FanOut(Zipf()))],
            id="db_default_on_the_key_this_package_fills",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (GeneratedSeat,),
            lambda: [
                _companies(),
                Table(GeneratedSeat, rows=20, company=FanOut(Zipf()), number=Sequential(1, 1)),
            ],
            id="generated_column_counted_together",
            marks=_NEEDS_DJANGO_5,
        ),
        # Conditions a left-out column stays outside, or that nothing can read.
        pytest.param(
            (DefaultOffRule,),
            lambda: [_companies(), Table(DefaultOffRule, rows=200, company=FanOut(Zipf()))],
            id="db_default_outside_the_condition",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (TokenCondition,),
            lambda: [_companies(), Table(TokenCondition, rows=200, company=FanOut(Zipf()))],
            id="expression_db_default_under_is_null",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (KeyRule,),
            lambda: [_companies(), Table(KeyRule, rows=200, company=FanOut(Zipf()))],
            id="condition_on_the_key",
        ),
        # Now() is one value and no more than that: a proof about the other column holds,
        # one row cannot collide, and a value that is never NULL stays outside IS NULL.
        pytest.param(
            (StampedFolder,),
            lambda: [Table(StampedFolder, rows=200, parent=Tree(roots=0.05, sizes=Constant(1)))],
            id="now_beside_a_flat_tree",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (StampedSeat,),
            lambda: [
                _companies(),
                Table(StampedSeat, rows=20, company=FanOut(Zipf(), max_children=1)),
            ],
            id="now_beside_a_cap_of_one",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (CodedUnique,),
            lambda: [Table(CodedUnique, rows=1)],
            id="unique_column_with_a_literal_db_default_on_one_row",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (StampedUnique,),
            lambda: [Table(StampedUnique, rows=1)],
            id="unique_column_with_now_on_one_row",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (TokenedUnique,),
            lambda: [Table(TokenedUnique, rows=20)],
            id="unique_column_with_an_expression_new_in_every_row",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (CodedRepeating,),
            lambda: [Table(CodedRepeating, rows=20)],
            id="a_column_not_unique_repeats_its_db_default",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (NullableUnique,),
            lambda: [Table(NullableUnique, rows=20)],
            id="unique_column_left_out_holds_a_null_per_row",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (CodedUnique,),
            lambda: [Table(CodedUnique, rows=20, code=Sequential(1, 1))],
            id="unique_column_declared_despite_its_db_default",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (StampedOnly,),
            lambda: [Table(StampedOnly, rows=1)],
            id="now_on_one_row",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (JsonRule,),
            lambda: [_companies(), Table(JsonRule, rows=200, company=FanOut(Zipf()))],
            id="a_dict_db_default_outside_a_condition",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (UnstampedRule,),
            lambda: [_companies(), Table(UnstampedRule, rows=200, company=FanOut(Zipf()))],
            id="now_outside_a_condition_on_null",
            marks=_NEEDS_DJANGO_5,
        ),
        # A partition with no group of two leaves a condition nothing to count.
        pytest.param(
            (OnRule,),
            lambda: [
                _companies(),
                Table(
                    OnRule,
                    rows=20,
                    company=FanOut(Zipf(), max_children=1),
                    status=Skew({"on": 1, "off": 1}),
                ),
            ],
            id="cap_of_one_under_a_condition_drawn_per_row",
        ),
        pytest.param(
            (OnRule,),
            lambda: [
                _companies(),
                Table(
                    OnRule, rows=50, company=FanOut(Zipf(), max_children=1), status=Constant("on")
                ),
            ],
            id="cap_of_one_with_every_row_inside_the_condition",
        ),
        pytest.param(
            (OnRule,),
            lambda: [
                _companies(),
                Table(
                    OnRule,
                    rows=20,
                    company=FanOut(Zipf(), max_children=1, null=0.5),
                    status=Constant("on"),
                ),
            ],
            id="cap_of_one_with_a_null_share_under_a_condition",
        ),
        pytest.param(
            (OnRule,),
            lambda: [
                _companies(),
                Table(OnRule, rows=50, company=FanOut(Constant(1)), status=Constant("on")),
            ],
            id="flat_sizes_under_a_condition",
        ),
        pytest.param(
            (OnRuleBetweenTwo,),
            lambda: [
                _companies(),
                Table(Person, rows=50, name=Constant("ada")),
                Table(
                    OnRuleBetweenTwo,
                    rows=20,
                    company=FanOut(Zipf(), max_children=1),
                    person=FanOut(Zipf()),
                    status=Constant("on"),
                ),
            ],
            id="cap_of_one_beside_a_second_fan_out_under_a_condition",
        ),
        pytest.param(
            (BranchOnRule,),
            lambda: [
                _companies(),
                Table(
                    BranchOnRule,
                    rows=20,
                    parent=Tree(roots=0.05, sizes=Zipf()),
                    company=FanOut(Zipf(), max_children=1),
                    status=Constant("on"),
                ),
            ],
            id="cap_of_one_beside_a_tree_under_a_condition",
        ),
        pytest.param(
            (TiedOnRule,),
            lambda: [
                _companies(),
                Table(
                    TiedOnRule,
                    rows=20,
                    company=FanOut(Zipf(), max_children=1),
                    status=Constant("on"),
                ),
            ],
            id="cap_of_one_with_no_null_to_tie_under_a_condition",
            marks=_NEEDS_DJANGO_5,
        ),
        # The nulls reading: a cap of one still holds with nothing NULL to tie.
        pytest.param(
            (TiedSeat,),
            lambda: [
                _companies(),
                Table(TiedSeat, rows=20, company=FanOut(Zipf(), max_children=1)),
            ],
            id="tied_left_out_column_beside_a_cap_of_one",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (OneColumn,),
            lambda: [
                _companies(),
                Table(OneColumn, rows=20, company=FanOut(Zipf(), max_children=1)),
            ],
            id="cap_of_one_alone",
        ),
        pytest.param(
            (TiedSeat,),
            lambda: [
                _companies(),
                Table(
                    TiedSeat,
                    rows=20,
                    company=FanOut(Zipf(), null=0.5),
                    label=Sequential(1, 1),
                ),
            ],
            id="distinct_beside_a_tied_null_share",
            marks=_NEEDS_DJANGO_5,
        ),
        # A Distinct column leaves each group one row.
        pytest.param(
            (CodedRule,),
            lambda: [
                _companies(),
                Table(
                    CodedRule,
                    rows=200,
                    company=FanOut(Zipf()),
                    code=Sequential(1, 1),
                    status=Skew({"on": 1, "off": 1}),
                ),
            ],
            id="distinct_under_a_condition",
        ),
        pytest.param(
            (CodedRule,),
            lambda: [
                _companies(),
                Table(
                    CodedRule,
                    rows=200,
                    company=FanOut(Zipf()),
                    code=Sequential(1, 1),
                    status=PerParent("company", last="on", rest="on"),
                ),
            ],
            id="distinct_under_a_condition_whatever_the_per_parent_says",
        ),
        pytest.param(
            (TiedCodedRule,),
            lambda: [
                _companies(),
                Table(
                    TiedCodedRule,
                    rows=200,
                    company=FanOut(Zipf(), null=0.3),
                    code=Sequential(1, 1),
                    status=Skew({"on": 1, "off": 1}),
                ),
            ],
            id="distinct_under_a_tied_condition_with_a_null_share",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (Triple,),
            lambda: [
                _companies(),
                _people(),
                Table(
                    Triple,
                    rows=200,
                    company=FanOut(Zipf()),
                    person=FanOut(Zipf()),
                    code=Sequential(1, 1),
                ),
            ],
            id="distinct_beside_two_fan_outs",
        ),
        pytest.param(
            (TiedTriple,),
            lambda: [
                _companies(),
                _people(),
                Table(
                    TiedTriple,
                    rows=200,
                    company=FanOut(Zipf(), null=0.3),
                    person=FanOut(Zipf(), null=0.3),
                    code=Sequential(1, 1),
                ),
            ],
            id="distinct_beside_two_tied_null_shares",
            marks=_NEEDS_DJANGO_5,
        ),
        # A NULL in every row keeps every row out of the index, whichever check asks.
        pytest.param(
            (Triple,),
            lambda: [
                _companies(5),
                _people(5),
                Table(Triple, rows=200, company=FanOut(Zipf()), person=FanOut(Zipf())),
            ],
            id="null_in_every_row_beside_two_fan_outs",
        ),
        pytest.param(
            (LabelledRule,),
            lambda: [
                _companies(),
                Table(
                    LabelledRule,
                    rows=200,
                    company=FanOut(Zipf()),
                    status=Skew({"on": 1, "off": 1}),
                ),
            ],
            id="null_in_every_row_under_a_condition",
        ),
        # A flat tree keeps the constraint on its own.
        pytest.param(
            (TreeTriple,),
            lambda: [
                _companies(5),
                _people(5),
                Table(
                    TreeTriple,
                    rows=200,
                    parent=Tree(roots=0.05, sizes=Constant(1)),
                    company=FanOut(Zipf()),
                    person=FanOut(Zipf()),
                ),
            ],
            id="flat_tree_beside_two_fan_outs",
        ),
        # One root leaves one NULL, so nothing ties under nulls_distinct=False.
        pytest.param(
            (TiedTreeTriple,),
            lambda: [
                _companies(5),
                _people(5),
                Table(
                    TiedTreeTriple,
                    rows=200,
                    parent=_one_root_chain(),
                    company=FanOut(Zipf()),
                    person=FanOut(Zipf()),
                ),
            ],
            id="one_root_beside_two_fan_outs",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (TiedChain,),
            lambda: [Table(TiedChain, rows=200, previous=_one_root_chain())],
            id="one_root_alone",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (TiedChain,),
            lambda: [Table(TiedChain, rows=2, previous=Tree(roots=0.5, sizes=Zipf()))],
            id="one_root_and_one_child_whatever_the_sizes",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (TiedNamed,),
            lambda: [Table(TiedNamed, rows=200, parent=_one_root_chain(), name=Constant("same"))],
            id="one_root_beside_a_draw",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (TiedNamed,),
            lambda: [Table(TiedNamed, rows=200, parent=_one_root_chain())],
            id="one_root_beside_a_left_out_column",
            marks=_NEEDS_DJANGO_5,
        ),
        pytest.param(
            (TiedAnswer,),
            lambda: [
                Table(TiedAnswer, rows=200, parent=_one_root_chain(), status=Constant("accepted"))
            ],
            id="one_root_in_a_conditional",
            marks=_NEEDS_DJANGO_5,
        ),
    ],
)
def test_each_exemption_is_accepted_and_loads(owned: Any, tables: Any) -> None:
    _assert_builds(owned, *tables())


@_ON_POSTGRES
@_NEEDS_DJANGO_5
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("model", "column", "table", "distinct"),
    [
        pytest.param(
            StampedFolder,
            "at",
            lambda: Table(StampedFolder, rows=200, parent=Tree(roots=0.05, sizes=Constant(1))),
            1,
            id="now_is_one_instant",
        ),
        pytest.param(
            TokenFolder,
            "token",
            lambda: Table(TokenFolder, rows=200, parent=Tree(roots=0.05, sizes=Zipf())),
            200,
            id="gen_random_uuid_is_new_in_every_row",
        ),
    ],
)
def test_what_an_expression_db_default_writes_across_one_copy(
    model: type[models.Model], column: str, table: Any, distinct: int
) -> None:
    # The premise under the Now reading, read off the table rather than off a collision.
    quote = connection.ops.quote_name
    with connection.schema_editor() as editor:
        editor.create_model(model)
    try:
        build_shape(Shape(table()))
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT COUNT(DISTINCT {quote(column)}) FROM {quote(model._meta.db_table)}"
            )
            assert cursor.fetchone()[0] == distinct
    finally:
        with connection.schema_editor() as editor:
            editor.delete_model(model)


def test_one_row_cannot_collide_with_anything() -> None:
    Shape(Table(Coupon, rows=1, batch=Skew({"a": 1, "b": 1}), code=Skew({"a": 1, "b": 1})))


# Under nulls_distinct=False the rows of a null share are one group, and a remedy that keeps
# the share is refused again. Each case is a table of its own, since the draws are keyed on its
# name, and each figure was measured over twenty seeds with the check switched off.
_SHARE = FanOut(Zipf(), null=0.1)


def _tied(name: str, fields: tuple[str, ...], columns: dict[str, Any], **sized: Any) -> _Case:
    return _Case(name, fields, None, columns, together=True, **sized)


@_NEEDS_DJANGO_5
def test_a_pairing_whose_relation_ties_its_nulls_is_refused() -> None:
    # Loads 0 of 20, and 20 of 20 with no null share or with each NULL apart.
    message = _refusal(
        _tied(
            "tied_two_fan_outs_paired_null_kept",
            ("company", "owner"),
            {"company": _SHARE, "owner": Paired("company", Zipf())},
            people=300,
        )
    )

    assert "is paired inside each group of" in message
    assert "with no null share on company" in message


@_ON_POSTGRES
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _tied(
                "tied_two_fan_outs_paired_no_null",
                ("company", "owner"),
                {"company": FanOut(Zipf()), "owner": Paired("company", Zipf())},
                people=300,
            ),
            marks=_NEEDS_DJANGO_5,
            id="tied_no_null_share",
        ),
        pytest.param(
            _Case(
                "apart_two_fan_outs_paired_null_kept",
                ("company", "owner"),
                None,
                {"company": _SHARE, "owner": Paired("company", Zipf())},
                people=300,
            ),
            id="apart",
        ),
    ],
)
def test_a_pairing_keeps_its_pairs_where_its_relation_s_nulls_are_no_group(case: _Case) -> None:
    _assert_loads(case)


@_NEEDS_DJANGO_5
@_ON_POSTGRES
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("refused", "offered", "builds"),
    [
        pytest.param(
            # Loads 0 of 20.
            _tied(
                "tied_two_fan_outs",
                ("company", "owner"),
                {"company": _SHARE, "owner": FanOut(Zipf())},
                people=300,
            ),
            "Paired('company', Zipf()) with no null share on company",
            _tied(
                "tied_two_fan_outs_paired_no_null",
                ("company", "owner"),
                {"company": FanOut(Zipf()), "owner": Paired("company", Zipf())},
                people=300,
            ),
            id="paired",
        ),
        pytest.param(
            # Loads 0 of 20.
            _tied(
                "tied_fan_out_beside_a_draw_with_room",
                ("company", "label"),
                {"company": _SHARE, "label": _skew()},
                companies=100,
            ),
            "scope='group') over company with no null share",
            _tied(
                "tied_fan_out_beside_a_draw_derived_no_null",
                ("company", "label"),
                {
                    "company": FanOut(Zipf()),
                    "label": Derived("company", compute=_named_for_position, scope="group"),
                },
            ),
            id="derived",
        ),
        pytest.param(
            # Loads 1 of 20, and 1 of 20 again under a cap that keeps the null share.
            _tied(
                "tied_tree_beside_a_fan_out",
                ("prev", "company"),
                {"prev": _tree(), "company": _SHARE},
                companies=300,
            ),
            "max_children=1 and no null share",
            _tied(
                "tied_tree_beside_a_fan_out_cap_no_null",
                ("prev", "company"),
                {"prev": _tree(), "company": FanOut(Zipf(), max_children=1)},
                companies=300,
            ),
            id="cap",
        ),
    ],
)
def test_a_remedy_offered_over_a_tied_null_share_drops_the_share(
    refused: _Case, offered: str, builds: _Case
) -> None:
    assert offered in _refusal(refused)
    _assert_loads(builds)


def test_a_group_derivation_is_refused_over_a_null_share_even_with_each_null_apart() -> None:
    # Loads 0 of 20. Why the Derived offer drops the share whichever way NULLs are counted.
    case = _Case(
        "apart_fan_out_beside_a_draw",
        ("company", "label"),
        None,
        {"company": _SHARE, "label": _skew()},
    )
    model = _model(case)

    assert "with no null share" in _refusal(case)
    with pytest.raises(InvalidShape, match="Drop the null share"):
        Table(
            model,
            rows=200,
            company=_SHARE,
            label=Derived("company", compute=_named_for_position, scope="group"),
        )


def test_a_cap_of_one_over_fewer_parents_than_rows_is_no_proof() -> None:
    # The build refuses the partition at every seed: 50 parents hold 50 rows at most.
    message = _refusal(
        _Case(
            "tree_beside_a_fan_out_cap_rows_over_parents",
            ("prev", "company"),
            None,
            {"prev": _tree(), "company": FanOut(Zipf(), max_children=1)},
        )
    )

    assert "says so with max_children=1" not in message
    assert "Projection" in message


@pytest.mark.parametrize(
    "company",
    [
        pytest.param(FanOut(Zipf()), id="skewed"),
        pytest.param(FanOut(Zipf(), max_children=1), id="capped"),
    ],
)
def test_a_one_to_one_over_fewer_parents_than_rows_is_not_offered_a_cap(company: FanOut) -> None:
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(30),
            Table(
                Ticketed,
                rows=50,
                prefix=Constant("p"),
                reference=Sequential("r", "x"),
                company=company,
            ),
        )

    assert "one row at most" in str(raised.value)
    assert "filled by max_children=1" not in str(raised.value)


@_ON_POSTGRES
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _Case(
                "apart_constant_none_beside_a_fan_out",
                ("company", "label"),
                None,
                {"company": FanOut(Zipf()), "label": Constant(None)},
            ),
            id="beside_a_fan_out",
        ),
        pytest.param(
            _Case("apart_constant_none_alone", ("label",), None, {"label": Constant(None)}),
            id="alone",
        ),
    ],
)
def test_a_declared_null_in_every_row_keeps_every_row_apart(case: _Case) -> None:
    # Each loads 20 of 20, and each was refused as one value. Tied, it is one value: 0 of 20.
    _assert_loads(case)


@_NEEDS_DJANGO_5
def test_a_declared_null_in_every_row_is_one_value_where_nulls_are_tied() -> None:
    _refusal(
        _tied(
            "tied_constant_none_beside_a_fan_out",
            ("company", "label"),
            {"company": FanOut(Zipf()), "label": Constant(None)},
        )
    )


def test_a_condition_grouped_by_a_plain_column_is_refused() -> None:
    # Was one of Booking's skips. Loads 0 of 20 on testapp_bk_grouped_by_a_plain_column, the
    # sizes below: every row draws its room without seeing another, and no column partitions.
    with isolate_apps("tests.testapp"):
        model = cast(
            "type[models.Model]",
            type(
                "bk_grouped_by_a_plain_column",
                (models.Model,),
                {
                    "__module__": __name__,
                    "company": models.ForeignKey(
                        Company, on_delete=models.CASCADE, related_name="+"
                    ),
                    "room": models.CharField(max_length=20),
                    "state": models.CharField(max_length=20),
                    "seats": models.IntegerField(),
                    "Meta": type(
                        "Meta",
                        (),
                        {
                            "app_label": "testapp",
                            "constraints": [
                                models.UniqueConstraint(
                                    fields=["room"],
                                    condition=models.Q(state="HELD"),
                                    name="u_bk_grouped_by_a_plain_column",
                                )
                            ],
                        },
                    ),
                },
            ),
        )
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(50),
            Table(
                model,
                rows=2000,
                company=FanOut(Zipf()),
                room=Skew({"one": 1, "two": 1}),
                state=Skew({"HELD": 0.5, "PAID": 0.5}),
                seats=Constant(0),
            ),
        )

    assert "u_bk_grouped_by_a_plain_column" in str(raised.value)
    assert "No column of the constraint is partitioned" in str(raised.value)


def test_several_special_rows_outside_the_condition_are_accepted() -> None:
    # count= puts ARCHIVED on three rows of each company and COMPLETE on the rest, so no row
    # is ACTIVE and the partial index holds none of them.
    Shape(
        _companies(50),
        _projects(
            2000,
            status=PerParent("company", last="ARCHIVED", rest="COMPLETE", count=3),
        ),
    )


def test_holding_null_is_not_offered_for_a_column_that_cannot_be_null() -> None:
    with isolate_apps("tests.testapp"):
        model = cast(
            "type[models.Model]",
            type(
                "cc_required_condition_column",
                (models.Model,),
                {
                    "__module__": __name__,
                    "company": models.ForeignKey(
                        Company, on_delete=models.CASCADE, related_name="+"
                    ),
                    "state": models.CharField(max_length=20),
                    "other": models.CharField(max_length=20),
                    "Meta": type(
                        "Meta",
                        (),
                        {
                            "app_label": "testapp",
                            "constraints": [
                                models.UniqueConstraint(
                                    fields=["company"],
                                    condition=models.Q(state=models.F("other")),
                                    name="u_cc_required_condition_column",
                                )
                            ],
                        },
                    ),
                },
            ),
        )
    with pytest.raises(InvalidShape) as raised:
        Shape(
            _companies(50),
            Table(
                model,
                rows=200,
                company=FanOut(Zipf()),
                state=Skew({"a": 1, "b": 1}),
                other=Skew({"a": 1, "b": 1}),
            ),
        )

    assert "is no set of values a PerParent could put" in str(raised.value)
    assert "Constant(None)" not in str(raised.value)


_WITHOUT_ITS_RELATION = {"company": FanOut(Zipf()), "owner": _paired()}
_TIED_SHARE = {"company": FanOut(Zipf(), null=0.1)}

# A Paired under a uniqueness that does not name company, two hundred rows over fifty
# companies. Each loads 0 of 20 on its own table, every failure an IntegrityError.
_PAIRED_WITHOUT_ITS_RELATION = (
    _Case("paired_alone_without_its_relation", ("owner",), None, _WITHOUT_ITS_RELATION, people=300),
    _Case(
        "paired_alone_without_its_relation_tied",
        ("owner",),
        None,
        {**_TIED_SHARE, "owner": _paired()},
        people=300,
        together=True,
    ),
    _Case(
        "paired_beside_a_draw_without_its_relation_tied",
        ("owner", "label"),
        None,
        {**_TIED_SHARE, "owner": _paired(), "label": _skew()},
        people=300,
        together=True,
    ),
    _Case(
        "paired_beside_a_fan_out_without_its_relation",
        ("other", "owner"),
        None,
        {**_WITHOUT_ITS_RELATION, "other": FanOut(Zipf())},
        people=300,
    ),
    # Fewer partners than rows, and room for every row beside the label: 450 combinations.
    _Case(
        "paired_beside_a_draw_short_of_partners",
        ("owner", "label"),
        None,
        {**_WITHOUT_ITS_RELATION, "label": _skew()},
        people=150,
    ),
    # As many partners as rows, where a partner each is just room enough.
    _Case(
        "paired_beside_a_draw_over_as_many_partners",
        ("owner", "label"),
        None,
        {**_WITHOUT_ITS_RELATION, "label": _skew()},
        people=200,
    ),
    _Case(
        "paired_alone_on_a_required_column",
        ("owner",),
        None,
        _WITHOUT_ITS_RELATION,
        people=300,
        owner_required=True,
    ),
)


def _without_its_relation(name: str) -> _Case:
    return next(case for case in _PAIRED_WITHOUT_ITS_RELATION if case.name == name)


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(case, id=case.name, marks=[_NEEDS_DJANGO_5] if case.together else [])
        for case in _PAIRED_WITHOUT_ITS_RELATION
    ],
)
def test_a_pairing_without_its_relation_is_refused(case: _Case) -> None:
    message = _refusal(case)

    assert f"uq_{case.name}" in message
    assert f"{case.name}.owner is paired inside each group of {case.name}.company" in message
    assert "which the constraint does not name" in message


def test_a_pairing_over_parents_this_shape_does_not_build_is_refused() -> None:
    # One company or fifty, loaded by the caller: nothing here reads which. Fifty load 0 of 20
    # as paired_alone_without_its_relation.
    case = _Case("paired_over_parents_left_unbuilt", ("owner",), None, _WITHOUT_ITS_RELATION)
    with pytest.raises(InvalidShape, match="which the constraint does not name"):
        Shape(
            Table(Person, rows=300, name=Constant("pat")),
            Table(_model(case), rows=200, **case.columns),
        )


_FLAT = FanOut(Constant(1))
_CAP = FanOut(Zipf(), max_children=1)


# Each offer loads 20 of 20 on its own table, and each withheld one 0 of 20 where it is
# withheld: the flat and capped fan-outs over 150 partners, the column left out where its
# NULLs are one value.
@_ON_POSTGRES
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("refused", "offered", "withheld", "builds"),
    [
        pytest.param(
            "paired_alone_without_its_relation",
            ("owner=FanOut(Constant(1)), or a FanOut with max_children=1", "owner left out"),
            ("Distinct",),
            (
                ("paired_offer_flat", {"company": FanOut(Zipf()), "owner": _FLAT}),
                ("paired_offer_cap", {"company": FanOut(Zipf()), "owner": _CAP}),
                ("paired_offer_left_out", {"company": FanOut(Zipf())}),
            ),
            id="alone",
        ),
        pytest.param(
            "paired_alone_without_its_relation_tied",
            ("owner=FanOut(Constant(1)), or a FanOut with max_children=1",),
            ("left out", "Distinct"),
            (
                ("paired_offer_flat_tied", {**_TIED_SHARE, "owner": _FLAT}),
                ("paired_offer_cap_tied", {**_TIED_SHARE, "owner": _CAP}),
            ),
            id="alone_tied",
            marks=[_NEEDS_DJANGO_5],
        ),
        pytest.param(
            "paired_beside_a_draw_without_its_relation_tied",
            ("owner=FanOut(Constant(1)), or a FanOut with max_children=1", "Distinct"),
            ("left out",),
            (
                (
                    "paired_offer_flat_beside_a_draw_tied",
                    {**_TIED_SHARE, "owner": _FLAT, "label": _skew()},
                ),
                (
                    "paired_offer_cap_beside_a_draw_tied",
                    {**_TIED_SHARE, "owner": _CAP, "label": _skew()},
                ),
                (
                    "paired_offer_distinct_beside_it_tied",
                    {**_TIED_SHARE, "owner": _paired(), "label": Sequential(1, 1)},
                ),
            ),
            id="beside_a_draw_tied",
            marks=[_NEEDS_DJANGO_5],
        ),
        pytest.param(
            "paired_beside_a_fan_out_without_its_relation",
            ("owner=FanOut(Constant(1)), or a FanOut with max_children=1", "owner left out"),
            ("Distinct",),
            (
                (
                    "paired_offer_flat_beside_a_fan_out",
                    {"company": FanOut(Zipf()), "other": FanOut(Zipf()), "owner": _FLAT},
                ),
            ),
            id="beside_a_fan_out",
        ),
        pytest.param(
            "paired_beside_a_draw_short_of_partners",
            ("owner left out", "Distinct"),
            ("FanOut(Constant(1))", "max_children=1"),
            (
                (
                    "paired_offer_left_out_short_of_partners",
                    {"company": FanOut(Zipf()), "label": _skew()},
                ),
            ),
            id="short_of_partners",
        ),
        pytest.param(
            "paired_beside_a_draw_over_as_many_partners",
            ("owner=FanOut(Constant(1)), or a FanOut with max_children=1", "Distinct"),
            (),
            (
                (
                    "paired_offer_flat_over_as_many_partners",
                    {"company": FanOut(Zipf()), "owner": _FLAT, "label": _skew()},
                ),
                (
                    "paired_offer_cap_over_as_many_partners",
                    {"company": FanOut(Zipf()), "owner": _CAP, "label": _skew()},
                ),
            ),
            id="as_many_partners",
        ),
        pytest.param(
            "paired_alone_on_a_required_column",
            ("owner=FanOut(Constant(1)), or a FanOut with max_children=1",),
            ("left out", "Distinct"),
            (),
            id="required",
        ),
    ],
)
def test_a_pairing_without_its_relation_is_offered_only_what_builds(
    refused: str,
    offered: tuple[str, ...],
    withheld: tuple[str, ...],
    builds: tuple[tuple[str, dict[str, Any]], ...],
) -> None:
    case = _without_its_relation(refused)
    message = _refusal(case)

    for one in (*offered, "a Projection with columns= and sql="):
        assert one in message
    for one in withheld:
        assert one not in message
    # Each offer on the refused table's sizes and constraint, the columns changed.
    for name, columns in builds:
        _assert_loads(case._replace(name=name, columns=columns))


def test_a_fan_out_of_one_row_per_partner_is_offered_where_the_partners_are_unknown() -> None:
    # People loaded by the caller are however many there are, so the offer stands on the
    # condition it states, as a cap does over a parent this shape does not build.
    case = _without_its_relation("paired_alone_without_its_relation")
    with pytest.raises(InvalidShape) as raised:
        Shape(_companies(50), Table(_model(case), rows=200, **case.columns))

    assert "owner=FanOut(Constant(1)), or a FanOut with max_children=1" in str(raised.value)


# Each loads 20 of 20: the relation in the constraint, and a relation of one parent, pinned
# by parents= or by one company declared, beside a draw under nulls counted as one as well.
@_ON_POSTGRES
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "case",
    [
        pytest.param(case, id=case.name, marks=[_NEEDS_DJANGO_5] if case.together else [])
        for case in (
            _Case(
                "paired_with_its_relation_unconditional",
                ("company", "owner"),
                None,
                _WITHOUT_ITS_RELATION,
                people=300,
            ),
            _Case(
                "paired_alone_over_one_parent_unconditional",
                ("owner",),
                None,
                {"company": FanOut(Zipf(), parents=(1,)), "owner": _paired()},
                people=300,
            ),
            _Case(
                "paired_alone_over_one_declared_parent_unconditional",
                ("owner",),
                None,
                _WITHOUT_ITS_RELATION,
                companies=1,
                people=300,
            ),
            _Case(
                "paired_beside_a_draw_over_one_parent_tied",
                ("owner", "label"),
                None,
                {"company": FanOut(Zipf(), parents=(1,)), "owner": _paired(), "label": _skew()},
                people=300,
                together=True,
            ),
        )
    ],
)
def test_a_pairing_with_its_relation_is_accepted_and_loads(case: _Case) -> None:
    _assert_loads(case)


@_ON_POSTGRES
@pytest.mark.django_db(transaction=True)
def test_a_pairing_beside_a_kept_pair_is_accepted_and_loads() -> None:
    # reviewer is paired over team, which the constraint does not name, and owner over
    # company, which it does: (company, owner) is distinct already. Loads 20 of 20.
    with isolate_apps("tests.testapp"):
        model = cast(
            "type[models.Model]",
            type(
                "cc_paired_beside_a_kept_pair",
                (models.Model,),
                {
                    "__module__": __name__,
                    "company": models.ForeignKey(
                        Company, on_delete=models.CASCADE, related_name="+"
                    ),
                    "owner": models.ForeignKey(Person, on_delete=models.CASCADE, related_name="+"),
                    "team": models.ForeignKey(Company, on_delete=models.CASCADE, related_name="+"),
                    "reviewer": models.ForeignKey(
                        Person, on_delete=models.CASCADE, related_name="+"
                    ),
                    "Meta": type(
                        "Meta",
                        (),
                        {
                            "app_label": "testapp",
                            "constraints": [
                                models.UniqueConstraint(
                                    fields=["company", "owner", "reviewer"],
                                    name="u_cc_paired_beside_a_kept_pair",
                                )
                            ],
                        },
                    ),
                },
            ),
        )
    _assert_builds(
        (model,),
        _companies(50),
        Table(Person, rows=300, name=Constant("pat")),
        Table(
            model,
            rows=200,
            company=FanOut(Zipf()),
            owner=Paired("company", Zipf()),
            team=FanOut(Zipf()),
            reviewer=Paired("team", Zipf()),
        ),
    )


_TWENTY = tuple(range(20))

# A key can be filled by a FanOut, a Paired or a Tree and by no distribution, so a refusal that
# says "a column of the constraint that is distinct in every row" has to have a column that is
# not one. A key left out of the shape reads as drawn per row, and was offered Distinct.
_NO_COLUMN_TO_SAY_DISTINCT = (
    # The relation is in the condition and the other column is a key left out: the shape's
    # third refusal, _check_a_relation_in_a_condition.
    pytest.param(
        _Case(
            "relation_condition_beside_a_left_out_key",
            ("company", "prev"),
            Q(company=1),
            {"company": FanOut(Zipf())},
            together=True,
        ),
        ("parents= naming only keys outside the condition", "company left out of the shape"),
        (
            (
                "relation_condition_beside_a_left_out_key_narrowed",
                {"company": FanOut(Zipf(), parents=(2, 3))},
            ),
            ("relation_condition_beside_a_left_out_key_left_out", {}),
        ),
        id="relation_in_a_condition",
    ),
    # The pairing without its relation, beside a key left out whose NULLs are one value.
    pytest.param(
        _Case(
            "pairing_beside_a_left_out_key",
            ("owner", "other"),
            None,
            _WITHOUT_ITS_RELATION,
            together=True,
            people=300,
        ),
        ("owner=FanOut(Constant(1)), or a FanOut with max_children=1",),
        (
            ("pairing_beside_a_left_out_key_flat", {"company": FanOut(Zipf()), "owner": _FLAT}),
            ("pairing_beside_a_left_out_key_cap", {"company": FanOut(Zipf()), "owner": _CAP}),
        ),
        id="pairing_without_its_relation",
    ),
    # A condition over a draw, the only column of the constraint a key left out.
    pytest.param(
        _Case(
            "draw_condition_over_a_left_out_key",
            ("other",),
            Q(state="x"),
            {"state": Skew({"x": 0.1, "y": 0.9})},
            together=True,
        ),
        ("state left out of the shape",),
        (("draw_condition_over_a_left_out_key_left_out", {}),),
        id="condition_over_draws",
    ),
    # A fan-out beside a key left out, NULLs counted as one value: 40 rows over 50 companies.
    # Both remedies it used to offer fill the other column, and Table refuses each on a key, so
    # nothing is left but the statement, which test_the_statement_a_fan_out_beside_a_key_offers
    # builds.
    pytest.param(
        _Case(
            "fan_out_beside_a_left_out_key",
            ("company", "prev"),
            None,
            {"company": FanOut(Zipf())},
            rows=40,
            together=True,
        ),
        (),
        (),
        id="fan_out_beside_a_key",
    ),
    # A tree beside a key left out: the flat tree is the one remedy that is not the statement.
    pytest.param(
        _Case(
            "tree_beside_a_left_out_key",
            ("prev", "other"),
            None,
            {"prev": _tree()},
            together=True,
        ),
        ("a tree with one root, roots at most 1/201",),
        (("tree_beside_a_left_out_key_flat", {"prev": Tree(roots=1 / 201, sizes=Constant(1))}),),
        id="tree_beside_a_key",
    ),
    # A tree beside a fan-out and nothing else, where a cap of one is room for every row.
    pytest.param(
        _Case(
            "tree_beside_a_fan_out_alone",
            ("prev", "other"),
            None,
            {"prev": _tree(), "other": FanOut(Zipf())},
            rows=40,
            together=True,
        ),
        ("max_children=1", "a tree with one root, roots at most 1/41"),
        (
            (
                "tree_beside_a_fan_out_alone_flat",
                {"prev": Tree(roots=1 / 41, sizes=Constant(1)), "other": FanOut(Zipf())},
            ),
            (
                "tree_beside_a_fan_out_alone_cap",
                {"prev": _tree(), "other": FanOut(Zipf(), max_children=1)},
            ),
        ),
        id="tree_beside_a_fan_out",
    ),
    # The same pair beside a key left out, which is no column to make Distinct either.
    pytest.param(
        _Case(
            "tree_and_a_fan_out_beside_a_left_out_key",
            ("prev", "company", "other"),
            None,
            {"prev": _tree(), "company": FanOut(Zipf())},
            rows=40,
            together=True,
        ),
        ("max_children=1", "a tree with one root, roots at most 1/41"),
        (
            (
                "tree_and_a_fan_out_beside_a_left_out_key_flat",
                {"prev": Tree(roots=1 / 41, sizes=Constant(1)), "company": FanOut(Zipf())},
            ),
            (
                "tree_and_a_fan_out_beside_a_left_out_key_cap",
                {"prev": _tree(), "company": FanOut(Zipf(), max_children=1)},
            ),
        ),
        id="tree_and_a_fan_out_beside_a_key",
    ),
    # A condition over a tree beside a key left out: the column filled so no row lands inside
    # the condition, and the flat tree.
    pytest.param(
        _Case(
            "condition_over_a_tree_beside_a_left_out_key",
            ("prev", "other"),
            Q(state="x"),
            {"prev": _tree(), "state": Skew({"x": 0.1, "y": 0.9})},
            together=True,
        ),
        ("state filled so that no row lands inside the condition", "roots at most 1/201"),
        (
            (
                "condition_over_a_tree_beside_a_left_out_key_clear",
                {"prev": _tree(), "state": Constant("y")},
            ),
            (
                "condition_over_a_tree_beside_a_left_out_key_flat",
                {
                    "prev": Tree(roots=1 / 201, sizes=Constant(1)),
                    "state": Skew({"x": 0.1, "y": 0.9}),
                },
            ),
        ),
        id="condition_over_a_tree",
    ),
)


# Each refused table is the three offers' origin; each remedy it still offers loads 20 of 20 on
# the refused table's sizes, and the Distinct it used to offer for a key could not be declared
# (Table: "is a relation, so it needs a FanOut rather than a value distribution").
@_ON_POSTGRES
@_NEEDS_DJANGO_5
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("refused", "offered", "builds"), _NO_COLUMN_TO_SAY_DISTINCT)
def test_distinct_is_offered_only_over_a_column_that_can_say_it(
    refused: _Case,
    offered: tuple[str, ...],
    builds: tuple[tuple[str, dict[str, Any]], ...],
) -> None:
    message = _refusal(refused)

    assert "Distinct" not in message
    for one in (*offered, "a Projection with columns= and sql="):
        assert one in message
    for name, columns in builds:
        _assert_loads(refused._replace(name=name, columns=columns), _TWENTY)


# The other side of the guard: beside a column that is not a key, Distinct is still offered, and
# declared on that column it loads. The two tree refusals have no other test that holds it.
_A_COLUMN_THAT_CAN_SAY_DISTINCT = (
    pytest.param(
        _Case(
            "tree_and_a_fan_out_beside_a_draw",
            ("prev", "company", "label"),
            None,
            {"prev": _tree(), "company": FanOut(Zipf()), "label": _skew()},
            rows=40,
            together=True,
        ),
        id="tree_beside_a_partition",
    ),
    pytest.param(
        _Case(
            "condition_over_a_tree_beside_a_draw",
            ("prev", "label"),
            Q(state="x"),
            {"prev": _tree(), "state": Skew({"x": 0.1, "y": 0.9}), "label": _skew()},
            together=True,
        ),
        id="condition_over_a_tree",
    ),
)


@_ON_POSTGRES
@_NEEDS_DJANGO_5
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("refused", _A_COLUMN_THAT_CAN_SAY_DISTINCT)
def test_distinct_is_still_offered_over_a_column_that_can_say_it(refused: _Case) -> None:
    assert "Distinct -- Sequential does" in _refusal(refused)

    _assert_loads(
        refused._replace(
            name=f"{refused.name}_distinct",
            columns={**refused.columns, "label": Sequential(1, 1)},
        ),
        _TWENTY,
    )


def test_a_key_cannot_be_declared_distinct() -> None:
    # The claim the refusals above rest on: the offer was for a declaration Table refuses.
    with pytest.raises(InvalidShape, match="is a relation, so it needs a FanOut"):
        Table(_model(_PER_PARENT_OVER_A_NULL_SHARE), rows=10, company=Sequential(1, 1))


def test_a_key_cannot_be_derived_either() -> None:
    # The other remedy the fan-out beside a draw offers, which fills the draw column.
    with pytest.raises(InvalidShape, match="is a relation, so it needs a FanOut.*or a derivation"):
        Table(
            _model(_PER_PARENT_OVER_A_NULL_SHARE),
            rows=10,
            company=FanOut(Zipf()),
            prev=Derived("company", compute=_named_for_position, scope="group"),
        )


_FAN_OUT_BESIDE_A_KEY = _Case(
    "fan_out_beside_a_key_statement",
    ("company", "prev"),
    None,
    {"company": FanOut(Zipf())},
    rows=40,
    together=True,
)


def test_a_fan_out_beside_a_key_is_offered_the_statement_alone() -> None:
    message = _refusal(_FAN_OUT_BESIDE_A_KEY)

    assert "Derived" not in message
    assert "Every other column of the constraint is a key" in message
    assert "filled by a Projection with columns= and sql=" in message
    # A column that can take either is still offered both.
    assert "Derived('company', compute=..., scope='group')" in _refusal(
        _FAN_OUT_BESIDE_A_KEY._replace(
            name="fan_out_beside_a_draw_statement",
            fields=("company", "label"),
            columns={"company": FanOut(Zipf()), "label": _skew()},
        )
    )


@_ON_POSTGRES
@_NEEDS_DJANGO_5
@pytest.mark.django_db(transaction=True)
def test_the_statement_a_fan_out_beside_a_key_offers_builds() -> None:
    case = _FAN_OUT_BESIDE_A_KEY._replace(name="fan_out_beside_a_key_projection")
    model = _model(case)
    quote = connection.ops.quote_name
    with connection.schema_editor() as editor:
        editor.create_model(model)
    try:
        for seed in _TWENTY:
            build_shape(
                Shape(
                    Table(Company, rows=50, name=Constant("acme")),
                    Projection(
                        model,
                        columns=("id", "company"),
                        sql=f"SELECT id, id FROM {quote(Company._meta.db_table)}",
                    ),
                    seed=seed,
                )
            )
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) FROM {quote(model._meta.db_table)}")
                assert cursor.fetchone()[0] == 50
                cursor.execute(
                    f"TRUNCATE {quote(Company._meta.db_table)}, {quote(model._meta.db_table)} CASCADE"
                )
    finally:
        with connection.schema_editor() as editor:
            editor.delete_model(model)


@_NEEDS_DJANGO_5
def test_a_constraint_over_keys_alone_is_met_by_the_pigeonhole_before_any_offer() -> None:
    # The drawn-columns refusal offers Distinct too, and no key reaches it: a key left out is a
    # constant of one value, so the arithmetic refuses first at two rows or more, and a key
    # declared is a fan-out, a tree or a Paired, which that refusal returns on.
    message = _refusal(_Case("keys_alone", ("other", "prev"), None, {}, together=True))

    assert "this shape can produce 1" in message
    assert "Distinct" not in message


# Measured over twenty seeds on the refused table's sizes: company=FanOut(Zipf(), null=0.3)
# under Q(state="x") is refused, and the same table with no null share and the PerParent the
# message offers loads 20 of 20, NULLs counted as one value.
_PER_PARENT_OVER_A_NULL_SHARE = _Case(
    "per_parent_over_a_null_share",
    ("company", "label"),
    Q(state="x"),
    {
        "company": FanOut(Zipf(), null=0.3),
        "state": Skew({"x": 0.1, "y": 0.9}),
        "label": _skew(),
    },
    together=True,
)


def test_a_per_parent_over_a_null_share_is_offered_only_with_the_share_dropped() -> None:
    message = _refusal(_PER_PARENT_OVER_A_NULL_SHARE)

    assert "PerParent('company', last='x', rest=...) over company with no null share" in message
    # What Table says of the PerParent the old wording offered, null share and all.
    with pytest.raises(InvalidShape, match="Drop the null share"):
        Table(
            _model(_PER_PARENT_OVER_A_NULL_SHARE),
            rows=200,
            company=FanOut(Zipf(), null=0.3),
            state=PerParent("company", last="x", rest="y"),
        )


def test_a_per_parent_over_a_fan_out_with_no_null_share_is_offered_without_the_clause() -> None:
    # Holds the null conjunct: the clause is for a share and for nothing else.
    message = _refusal(
        _PER_PARENT_OVER_A_NULL_SHARE._replace(
            name="per_parent_over_no_null_share",
            columns={**_PER_PARENT_OVER_A_NULL_SHARE.columns, "company": FanOut(Zipf())},
        )
    )

    assert "PerParent('company', last='x', rest=...)," in message
    assert "no null share" not in message


@_ON_POSTGRES
@_NEEDS_DJANGO_5
@pytest.mark.django_db(transaction=True)
def test_the_per_parent_the_refusal_offers_over_a_null_share_loads_with_the_share_dropped() -> None:
    _assert_loads(
        _PER_PARENT_OVER_A_NULL_SHARE._replace(
            name="per_parent_over_no_null_share_loads",
            columns={
                "company": FanOut(Zipf()),
                "state": PerParent("company", last="x", rest="y"),
                "label": _skew(),
            },
        ),
        _TWENTY,
    )


@pytest.mark.parametrize(
    ("columns", "fields", "rows"),
    [
        pytest.param(
            {"company": FanOut(Zipf(), null=0.1), "label": _skew()},
            ("company", "label"),
            40,
            id="beside_a_draw",
        ),
        pytest.param({"company": FanOut(Zipf(), null=0.1)}, ("company",), 40, id="alone"),
    ],
)
def test_the_nulls_a_refusal_counts_together_are_the_constraints_own_reading(
    columns: dict[str, Any], fields: tuple[str, ...], rows: int
) -> None:
    # Held by the ``_each_null_is_its_own_group`` conjunct in _a_null_share_counted_together:
    # a null share is the same fan-out either way, and the sentence about its rows being one
    # group is true only under nulls_distinct=False.
    apart = _Case("nulls_apart", fields, None, columns, rows=rows)
    refusal = _refusal(apart)
    assert "counts every NULL as one value" not in refusal
    if django.VERSION >= (5, 0):
        tied = _refusal(apart._replace(name="nulls_tied", together=True))
        assert "counts every NULL as one value under nulls_distinct=False" in tied
