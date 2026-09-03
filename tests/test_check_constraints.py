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
    Approval,
    Assignment,
    Booking,
    Company,
    Contest,
    Coupon,
    Escalation,
    Event,
    EventSession,
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
    # Eight skips in one model: a condition over an expression rather than
    # fields, one grouped by a column no fan-out partitions, one joining two
    # clauses with AND, an OR whose branches name two different columns, a
    # negated OR, an OR one of whose branches is a comparison, a comparison as a
    # keyword argument and the same comparison as a bare lookup expression --
    # which arrive as different shapes of child and take different exits -- plus
    # a check constraint, which is not a unique constraint at all.
    #
    # Two used to live here and moved out when they stopped being skips:
    # state__in to Review, and a nested Q(Q(a) | Q(b)) to Approval. What is left
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
