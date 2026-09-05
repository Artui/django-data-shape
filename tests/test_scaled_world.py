"""A world at one factor: built, handed over, and undone."""

from __future__ import annotations

import contextlib
import datetime

import pytest
from django.db import DEFAULT_DB_ALIAS, connection, connections
from django.test.utils import CaptureQueriesContext

from django_data_shape import (
    Constant,
    FanOut,
    Invariant,
    InvariantViolated,
    Shape,
    Skew,
    Table,
    Zipf,
    scaled_world,
)
from tests.testapp.models import Company, Order, Session

# No backend skip, unlike the loader's own tests, and the difference is the
# point: a growth harness asks for rows and cardinality rather than for a
# database the planner can reason about, so it builds on any backend and this
# module has to prove that on both. The one thing it must not do is assert
# anything about a plan.
#
# django_db rather than transaction=True, on purpose: the nested case is the one
# a consumer meets, and it is the one where the teardown has to be a savepoint
# rollback that leaves the test's own transaction alive. The transactional case
# gets a single test of its own at the bottom.
pytestmark = [pytest.mark.django_db]

_AWARE = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)


def _orders(rows: int = 10) -> Shape:
    return Shape(
        Table(
            Order,
            rows=rows,
            status=Skew({"complete": 0.9, "pending": 0.1}),
            total=Constant("1.00"),
            created_at=Constant(_AWARE),
        ),
        seed=99,
    )


def _graph(companies: int = 100, sessions: int = 1000) -> Shape:
    return Shape(
        Table(Company, rows=companies, name=Constant("acme")),
        Table(
            Session,
            rows=sessions,
            label=Constant("x"),
            company=FanOut(Zipf(), childless=0.3),
        ),
        seed=7,
    )


def test_the_world_exists_inside_the_block_and_is_gone_after() -> None:
    with scaled_world(_orders(rows=10), 3):
        assert Order.objects.count() == 30

    # The half that is easy to skip and the reason the harness is a context
    # manager at all: a world that outlived its block would be the next factor's
    # starting state, and the second measurement would be taken against the
    # first world plus the second.
    assert Order.objects.count() == 0


def test_it_yields_the_rows_the_database_took() -> None:
    with scaled_world(_orders(rows=10), 5) as rows:
        assert rows == 50 == Order.objects.count()


def test_a_second_factor_starts_from_an_empty_table() -> None:
    counts = []
    for factor in (1, 10):
        with scaled_world(_orders(rows=10), factor) as rows:
            counts.append(rows)

    # What a growth assertion actually runs, in miniature: the same declaration
    # at two sizes, each measured against a database holding only its own world.
    assert counts == [10, 100]


def test_a_failure_inside_the_block_leaves_nothing_behind() -> None:
    with pytest.raises(ZeroDivisionError), scaled_world(_orders(rows=10), 2):
        assert Order.objects.count() == 20
        1 / 0  # noqa: B018

    assert Order.objects.count() == 0


def test_the_enclosing_transaction_survives_the_teardown() -> None:
    # Undoing a world must not undo the test around it. The rollback is to the
    # savepoint the world was built inside, so a row written before the block is
    # still there afterwards and the connection is still usable -- which is not
    # what a caller would get from marking the whole transaction for rollback.
    before = Company.objects.create(name="written by the test")

    with scaled_world(_orders(rows=10), 1):
        pass

    assert Company.objects.filter(pk=before.pk).exists()
    assert Order.objects.count() == 0


def test_the_fan_out_keeps_its_shape_at_a_larger_factor() -> None:
    # Scaling every table is what makes two worlds differ only in size. Children
    # per parent is the number that would move if the parent table were held
    # fixed, and the childless tail is the part of the declaration a subset of
    # one build would quietly remove.
    measured = []
    for factor in (1, 4):
        with scaled_world(_graph(), factor):
            parents = Company.objects.count()
            childless = Company.objects.filter(sessions__isnull=True).count()
            measured.append((Session.objects.count() / parents, childless / parents))

    assert measured[0][0] == measured[1][0] == 10.0
    # A band rather than the declared 0.3 itself: the childless decision is a
    # draw per parent index, so a hundred parents land a couple of standard
    # deviations either side of it and pinning the number would be asserting on
    # the seed. What the band does catch is the two failures that matter -- a
    # tail that vanished, and a tail that is only there at one of the two sizes.
    for _, childless_share in measured:
        assert 0.1 < childless_share < 0.5


@pytest.mark.django_db(transaction=True)
def test_it_also_undoes_a_world_it_opened_the_transaction_for() -> None:
    # The other half of the claim that one implementation is correct in both
    # places: with no enclosing atomic block there is no savepoint to roll back
    # to, so the same exit rolls back the transaction the world was built in.
    with scaled_world(_orders(rows=10), 2) as rows:
        assert rows == 20

    assert Order.objects.count() == 0


# The statement cost of building a world, pinned rather than described. Both
# numbers were prose in a docstring until a consumer needed them and could not
# check them without taking the dependency the scale protocol exists to avoid --
# and the PostgreSQL one was wrong by two when it was finally measured. They are
# measurements, so a deliberate change to the loader may move them; what must
# not change silently is which of the two is a constant and which is a curve.
# Counted with CaptureQueriesContext inside a non-transactional django_db test,
# which is what pytestmark above gives every test in this module. Both of those
# choices move the number: through execute_wrapper the same shape is eleven, and
# a transaction=True test is one savepoint fewer. The constants are therefore a
# regression guard on this module's own measurement, not a published figure.
#
# It moved from fourteen to sixteen in 0.7.0, when statistics targets added one
# catalogue read per table -- a deliberate change to the loader, and exactly the
# kind these constants exist to make visible rather than silent. What did not
# move is the property being asserted: still fixed whatever the factor.
#
# It moved again when a scaled world began emptying the tables it declares, so a
# session world can sit under one. On PostgreSQL that is a single TRUNCATE
# whatever the shape and whatever the factor, so the Postgres constant gains one
# and stays a constant. Off PostgreSQL it is one DELETE per declared table --
# two here -- which is a property of the *declaration* and not of the factor, so
# the curve below still has the loader's own shape and only its intercept moved.
_POSTGRES_STATEMENTS = 17
_PORTABLE_STATEMENTS = 12
_ROWS_PER_INSERT = 1000


def _statements(shape: Shape, factor: int, alias: str) -> int:
    with (
        CaptureQueriesContext(connections[alias]) as captured,
        scaled_world(shape, factor, using=alias),
    ):
        pass
    return len(captured)


@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="the COPY route needs PostgreSQL to be measured"
)
def test_building_a_world_costs_the_same_on_postgres_at_every_factor() -> None:
    # The half that makes a capture around the block merely wrong rather than
    # catastrophic: COPY is not a wrapped statement, so the overhead is the
    # TRUNCATE, the emptiness check, the statistics-target read, the parent key
    # read, the sequence reset, the ANALYZE and the savepoints -- none of which
    # depend on how many rows there are.
    shape = _graph(companies=10, sessions=_ROWS_PER_INSERT)

    assert _statements(shape, 1, DEFAULT_DB_ALIAS) == _POSTGRES_STATEMENTS
    assert _statements(shape, 5, DEFAULT_DB_ALIAS) == _POSTGRES_STATEMENTS


@pytest.mark.django_db(databases=["default", "not_postgres"])
def test_and_a_cost_that_grows_with_the_factor_where_there_is_no_copy() -> None:
    # The half a growth assertion has to know about. Every insert here is a
    # wrapped statement, one per chunk, so a capture opened around the world
    # measures the loader's curve rather than the subject's -- and it is a curve
    # with the same shape as the one being asserted about, which is the worst
    # possible confound.
    shape = _graph(companies=10, sessions=_ROWS_PER_INSERT)

    at_one = _statements(shape, 1, "not_postgres")
    at_five = _statements(shape, 5, "not_postgres")

    assert at_one == _PORTABLE_STATEMENTS
    # Four more chunks of sessions; the ten companies still fit in one.
    assert at_five == _PORTABLE_STATEMENTS + 4


def test_a_scaled_world_is_checked_against_the_rules_the_shape_declared() -> None:
    """The symptom the declaration-level fix exists to prevent.

    Dropping the invariants left this call succeeding: the world built, the
    rule that says it is impossible never ran, and a growth assertion went on to
    measure a database its own declaration had already ruled out. Nothing raised
    and nothing warned, which is why it needs a test at this level and not only
    at the level of the shape that comes back.
    """
    impossible = Invariant("every company is a violation", sql="SELECT id FROM testapp_company")
    shape = Shape(Table(Company, rows=4, name=Constant("acme")), invariants=(impossible,))

    # entered through an ExitStack rather than a ``with`` body, because the
    # build raises on the way in and a body would be a line that never runs.
    with pytest.raises(InvariantViolated), contextlib.ExitStack() as entering:
        entering.enter_context(scaled_world(shape, 1))
