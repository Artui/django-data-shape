"""A world at one factor: built, handed over, and undone."""

from __future__ import annotations

import datetime

import pytest
from django.db import connection

from django_data_shape import Constant, FanOut, Shape, Skew, Table, Zipf, scaled_world
from tests.testapp.models import Company, Order, Session

# The same rule the loader's own tests follow: skipped with a stated reason
# rather than passed vacuously. Scaling a declaration is arithmetic and is
# tested without a database next door; building the world it describes is COPY,
# a reset sequence and ANALYZE, and none of those mean anything here.
#
# django_db rather than transaction=True, on purpose: the nested case is the one
# a consumer meets, and it is the one where the teardown has to be a savepoint
# rollback that leaves the test's own transaction alive. The transactional case
# gets a single test of its own at the bottom.
pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="COPY loading and planner statistics need PostgreSQL",
    ),
]

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
