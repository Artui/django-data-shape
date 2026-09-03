"""Per-group rules against a real database, and the two nets behind them.

Everything here needs PostgreSQL, and not only for ``COPY``: the constraint
that makes the worked example a worked example is a partial unique index, and a
backend that cannot express one would let every assertion below pass for the
wrong reason.
"""

from __future__ import annotations

import datetime
from typing import Any, cast

import pytest
from django.db import IntegrityError, connection
from django.db.models import Q

from django_data_shape import (
    Constant,
    Derived,
    FanOut,
    Invariant,
    InvariantViolated,
    PerParent,
    Sequential,
    Shape,
    Skew,
    Table,
    Zipf,
    check_invariants,
    shape_digest,
)
from django_data_shape import build as build_shape
from tests.testapp.models import (
    Booking,
    Company,
    Contest,
    Entry,
    Period,
    Project,
    Seat,
    Vendor,
)

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="COPY loading and partial unique constraints need PostgreSQL",
    ),
]

_START = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
_MINUTE = datetime.timedelta(minutes=1)
_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def _letter_for_position(group: object) -> str:
    """One letter per position inside a parent's group of children."""
    position, _size = cast("tuple[int, int]", group)
    return _LETTERS[position % len(_LETTERS)]


def _companies(rows: int = 50) -> Table:
    return Table(Company, rows=rows, name=Constant("acme"))


def _projects(rows: int = 2000, **overrides: Any) -> Table:
    fields: dict[str, Any] = {
        "company": FanOut(Zipf(1.2)),
        "created_at": Sequential(_START, _MINUTE),
        "status": PerParent("company", last="ACTIVE", rest="COMPLETE"),
    }
    fields.update(overrides)
    return Table(Project, rows=rows, fields=fields)


def _statuses_by_company() -> dict[int, list[str]]:
    """Every company's projects in created_at order, as their statuses."""
    grouped: dict[int, list[str]] = {}
    for company_id, status in Project.objects.order_by("created_at").values_list(
        "company_id", "status"
    ):
        grouped.setdefault(company_id, []).append(status)
    return grouped


def test_the_partial_unique_constraint_is_really_in_the_test_schema() -> None:
    # Without this the assertions below would pass just as happily against a
    # schema that never created the index -- which is a test passing for a
    # reason unrelated to its name, and the load would prove nothing at all.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = %s",
            ["one_active_project_per_company"],
        )
        row = cursor.fetchone()

    assert row is not None
    assert "WHERE" in row[0] and "ACTIVE" in row[0]


def test_one_active_project_per_company_holds_across_the_whole_table() -> None:
    build_shape(Shape(_companies(50), _projects(2000), seed=11))

    grouped = _statuses_by_company()

    assert sum(len(statuses) for statuses in grouped.values()) == 2000
    assert all(statuses.count("ACTIVE") == 1 for statuses in grouped.values())


def test_the_active_share_is_derived_from_the_fan_out_rather_than_declared() -> None:
    # The finding the whole feature rests on: status skew and fan-out are one
    # declaration seen twice. Fifty companies and two thousand projects means
    # exactly fifty active rows -- 2.5% -- and nobody chose that number.
    build_shape(Shape(_companies(50), _projects(2000), seed=11))

    active = Project.objects.filter(status="ACTIVE").count()
    with_projects = Project.objects.values("company_id").distinct().count()

    assert active == with_projects
    assert active / 2000 == pytest.approx(with_projects / 2000)


def test_a_childless_company_contributes_no_active_project() -> None:
    # The reason the count is one per *non-empty* group. A company nobody
    # references has no project to be its active one, and inventing one would
    # be inventing a row the fan-out said was not there.
    build_shape(
        Shape(
            _companies(50),
            _projects(200, company=FanOut(Zipf(1.4), childless=0.4)),
            seed=3,
        )
    )

    active = Project.objects.filter(status="ACTIVE").count()

    assert active == Project.objects.values("company_id").distinct().count()
    assert active < 50


def test_the_rule_holds_while_the_rows_are_still_emitted_interleaved() -> None:
    # Assignment order is not emission order, and this is the assertion that
    # says so. Under the honest default a company's projects are scattered
    # through the table -- consecutive rows come from unrelated parents -- and
    # the per-group rule still holds exactly.
    build_shape(Shape(_companies(50), _projects(500), seed=5))

    companies = list(Project.objects.order_by("id").values_list("company_id", flat=True))
    neighbours_from_one_company = sum(
        1 for before, after in zip(companies, companies[1:], strict=False) if before == after
    )

    assert neighbours_from_one_company < len(companies) // 10
    assert all(statuses.count("ACTIVE") == 1 for statuses in _statuses_by_company().values())


def test_ordering_a_group_makes_the_newest_project_the_active_one() -> None:
    # What order_by buys, and it is application realism rather than plan
    # realism: PostgreSQL keeps no statistic about which row of a group holds
    # which value, so the shape without it plans identically.
    build_shape(
        Shape(
            _companies(20),
            _projects(
                200,
                company=FanOut(Zipf(1.2), placement="grouped"),
                status=PerParent("company", last="ACTIVE", rest="COMPLETE", order_by="created_at"),
            ),
            seed=5,
        )
    )

    grouped = _statuses_by_company()

    assert grouped
    assert all(statuses[-1] == "ACTIVE" for statuses in grouped.values())
    assert all(statuses.count("ACTIVE") == 1 for statuses in grouped.values())


def test_several_winners_per_group_where_no_constraint_says_otherwise() -> None:
    build_shape(
        Shape(
            Table(Contest, rows=10, name=Constant("cup")),
            Table(
                Entry,
                rows=200,
                contest=FanOut(Zipf(1.2)),
                placing=PerParent("contest", last="WON", rest="LOST", count=3),
            ),
            seed=2,
        )
    )

    per_contest = {}
    for contest_id, placing in Entry.objects.values_list("contest_id", "placing"):
        per_contest.setdefault(contest_id, []).append(placing)

    assert per_contest
    # Three winners, unless the contest had fewer entries than that -- which is
    # arithmetic rather than a clamp: two entries cannot produce three winners.
    assert all(placings.count("WON") == min(3, len(placings)) for placings in per_contest.values())


def test_the_current_period_is_the_one_with_no_end() -> None:
    # The SCD-2 shape, and the reason last= needs a sentinel rather than
    # defaulting to None: here None *is* the declared value.
    build_shape(
        Shape(
            _companies(10),
            Table(
                Period,
                rows=100,
                company=FanOut(Zipf(1.2), placement="grouped"),
                valid_from=Sequential(_START, _MINUTE),
                valid_to=PerParent(
                    "company", last=None, rest=_START + datetime.timedelta(days=365)
                ),
            ),
            seed=4,
        )
    )

    per_company: dict[int, list[object]] = {}
    for company_id, valid_to in Period.objects.order_by("valid_from").values_list(
        "company_id", "valid_to"
    ):
        per_company.setdefault(company_id, []).append(valid_to)

    assert per_company
    assert all(ends[-1] is None for ends in per_company.values())
    assert all(ends.count(None) == 1 for ends in per_company.values())


def test_the_database_is_the_net_the_pre_check_cannot_be() -> None:
    # A conditional constraint grouped by a plain column: there is no partition
    # here to satisfy it with, so the pre-check skips it by design and says so.
    # This is what the skip costs, and it is the failure the pre-check exists to
    # replace where it can -- an index violation from inside the load.
    shape = Shape(
        _companies(5),
        Table(
            Booking,
            rows=5,
            company=FanOut(Constant(1)),
            room=Constant("one"),
            state=Constant("HELD"),
            seats=Constant(4),
        ),
        seed=1,
    )

    with pytest.raises(IntegrityError):
        build_shape(shape)


def test_a_column_distinct_in_every_row_really_does_keep_a_two_column_uniqueness() -> None:
    # The exemption, loaded rather than argued. ``one_seat_label_per_company``
    # is a real unique index here, and this is the declaration the refusal lets
    # through: a pair is distinct as soon as either half is, so nothing has to
    # be arranged per group at all.
    build_shape(
        Shape(
            _companies(50),
            Table(Seat, rows=100, company=FanOut(Zipf()), label=Sequential(0, 1)),
        )
    )

    assert Seat.objects.count() == 100


def test_the_remedy_the_refusal_names_is_one_that_builds() -> None:
    # The same table, the same constraint, and the form the message points at:
    # a Scope.GROUP derivation receives this row's position among its parent's
    # children, so it can hand back a value per position where a draw beside the
    # fan-out can only hope. Asserted against the database rather than taken on
    # trust -- a refusal naming a remedy that does not build would be worse than
    # one naming none.
    build_shape(
        Shape(
            _companies(50),
            Table(
                Seat,
                rows=100,
                company=FanOut(Zipf()),
                label=Derived("company", compute=_letter_for_position, scope="group"),
            ),
        )
    )

    assert Seat.objects.count() == 100


def test_an_invariant_that_finds_nothing_lets_the_build_finish() -> None:
    result = build_shape(
        Shape(
            _companies(20),
            _projects(200),
            seed=6,
            invariants=[
                Invariant(
                    "no company has two active projects",
                    sql="SELECT company_id FROM testapp_project WHERE status = 'ACTIVE' "
                    "GROUP BY company_id HAVING count(*) > 1",
                ),
                Invariant(
                    "no project predates the epoch this shape declares",
                    Project,
                    violated_by=Q(created_at__lt=_START),
                ),
            ],
        )
    )

    assert result.tables[-1].rows == 200


def test_a_violated_invariant_fails_the_build_and_rolls_it_back() -> None:
    with pytest.raises(InvariantViolated, match="every project is complete"):
        build_shape(
            Shape(
                _companies(20),
                _projects(200),
                seed=6,
                invariants=[
                    Invariant(
                        "every project is complete",
                        Project,
                        violated_by=Q(status="ACTIVE"),
                    )
                ],
            )
        )

    # The half that makes this a build failure rather than a test failure: a
    # database full of impossible data would make every later assertion pass or
    # fail for a reason unrelated to the code under test.
    assert Project.objects.count() == 0
    assert Company.objects.count() == 0


def test_a_violated_sql_invariant_quotes_the_rows_it_found() -> None:
    with pytest.raises(InvariantViolated, match="every project is complete") as raised:
        build_shape(
            Shape(
                _companies(20),
                _projects(200),
                seed=6,
                invariants=[
                    Invariant(
                        "every project is complete",
                        sql="SELECT id, status FROM testapp_project "
                        "WHERE status = 'ACTIVE' ORDER BY id",
                    )
                ],
            )
        )

    message = str(raised.value)
    assert "'ACTIVE'" in message
    assert "and more" in message
    assert "rolled back" in message


def test_a_failure_with_few_enough_offenders_lists_all_of_them() -> None:
    with pytest.raises(InvariantViolated) as raised:
        build_shape(
            Shape(
                _companies(3),
                _projects(30),
                seed=6,
                invariants=[
                    Invariant(
                        "no project is active",
                        sql="SELECT id FROM testapp_project WHERE status = 'ACTIVE' ORDER BY id",
                    )
                ],
            )
        )

    assert "and more" not in str(raised.value)


def test_the_rules_can_be_re_run_against_a_database_this_did_not_just_build() -> None:
    # Exported as well as called, because a template clone is built once and
    # cloned per test, and the rules are worth having against the clone.
    build_shape(Shape(_companies(20), _projects(200), seed=6))
    passing = Invariant(
        "no company has two active projects",
        sql="SELECT company_id FROM testapp_project WHERE status = 'ACTIVE' "
        "GROUP BY company_id HAVING count(*) > 1",
    )
    failing = Invariant("no project is active", Project, violated_by=Q(status="ACTIVE"))

    check_invariants(connection, [passing])

    with pytest.raises(InvariantViolated, match="no project is active"):
        check_invariants(connection, [passing, failing])


def test_an_invariant_reads_rows_a_default_manager_would_have_hidden() -> None:
    # _base_manager, for the reason a fan-out reads through it. Vendor's own
    # default manager hides retired rows, which is an entirely ordinary thing
    # for a project to write -- and a rule that could not see them would report
    # a database as clean in exactly the place it is not.
    build_shape(
        Shape(
            Table(Vendor, rows=100, name=Constant("v"), retired=Skew({True: 1, False: 1})),
            seed=6,
        )
    )
    retired = Invariant("no vendor is retired", Vendor, violated_by=Q(retired=True))

    assert Vendor.objects.filter(retired=True).count() == 0
    assert Vendor._base_manager.filter(retired=True).count() > 0

    with pytest.raises(InvariantViolated, match="no vendor is retired"):
        check_invariants(connection, [retired])


def test_an_invariant_changes_no_row_and_still_changes_the_cache_key() -> None:
    # Otherwise a shape would reuse a cached template database and the rule
    # would silently never run -- which is worse than no rule, because it is a
    # rule everybody believes.
    plain = Shape(_companies(20), _projects(200), seed=6)
    checked = Shape(
        _companies(20),
        _projects(200),
        seed=6,
        invariants=[Invariant("no project is active", Project, violated_by=Q(status="ACTIVE"))],
    )

    assert shape_digest(plain) != shape_digest(checked)


def test_a_shape_declaring_a_skew_against_the_constraint_never_reaches_the_load() -> None:
    # The pre-check is the point: this refusal costs nothing, where the same
    # declaration reaching PostgreSQL costs a partial load first.
    with pytest.raises(Exception, match="one_active_project_per_company"):
        Shape(_companies(50), _projects(2000, status=Skew({"ACTIVE": 0.1, "COMPLETE": 0.9})))
