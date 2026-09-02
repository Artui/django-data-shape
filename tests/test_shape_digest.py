"""The cache key: same shape, same answer; different shape, different answer."""

from __future__ import annotations

import datetime
import enum
import os
import subprocess
import sys
import textwrap
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from django_data_shape import (
    After,
    Aligned,
    Constant,
    Derived,
    FanOut,
    Given,
    KeyFunction,
    Projection,
    Sequential,
    SequentialKeys,
    Shape,
    Skew,
    Table,
    UnhashableShape,
    Uniform,
    UuidKeys,
    Zipf,
    shape_digest,
)
from tests.testapp.models import (
    Account,
    Company,
    Event,
    EventSession,
    Order,
    Session,
    SlugPk,
    Template,
    TemplateSession,
    Ticket,
)

# No database anywhere in this module, and no django_db marker. A content hash of
# a declaration is exactly the thing that must not need one: it is what decides
# whether a database is worth building at all, so it has to be answerable before
# any connection exists.

_AWARE = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
_ROOT = Path(__file__).resolve().parent.parent


class _Plan(enum.Enum):
    FREE = "free"
    PAID = "paid"


def _orders(rows: int = 100, seed: int = 0, **overrides: object) -> Shape:
    fields: dict[str, object] = {
        "status": Skew({"complete": 0.98, "pending": 0.02}),
        "total": Uniform(0, 500, places=2),
        "created_at": Sequential(_AWARE, datetime.timedelta(seconds=3)),
    }
    fields.update(overrides)
    return Shape(Table(Order, rows=rows, fields=fields), seed=seed)


def test_one_shape_hashes_to_one_answer() -> None:
    assert shape_digest(_orders()) == shape_digest(_orders())


def test_the_digest_is_hexadecimal_and_short_enough_to_name_a_database() -> None:
    digest = shape_digest(_orders())

    # PostgreSQL identifiers stop at 63 bytes, and a template's name is a prefix
    # plus this. A digest that did not fit would be found out by the database
    # silently truncating it -- which is two shapes sharing a database.
    assert len(digest) == 32
    assert set(digest) <= set("0123456789abcdef")


def test_two_processes_with_different_hash_seeds_agree() -> None:
    # The property the whole cache rests on, and the one Python's own hash()
    # does not have: it is salted per interpreter run for strings and bytes, so
    # a key built on it would be a different key in every process. Falsified by
    # replacing the blake2b in shape_digest with hash() -- the two runs below
    # then disagree and this fails, where every other test in this module still
    # passes because they all live in one process.
    script = textwrap.dedent(
        """
        import django

        django.setup()

        import datetime

        from django_data_shape import Constant, Sequential, Shape, Skew, Table, shape_digest
        from tests.testapp.models import Order

        print(
            shape_digest(
                Shape(
                    Table(
                        Order,
                        rows=17,
                        status=Skew({"a": 0.9, "b": 0.1}),
                        total=Constant("1.50"),
                        created_at=Sequential(
                            datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
                            datetime.timedelta(seconds=3),
                        ),
                    ),
                    seed=5,
                )
            )
        )
        """
    )
    answers = {
        subprocess.run(
            [sys.executable, "-c", script],
            cwd=_ROOT,
            capture_output=True,
            check=True,
            text=True,
            # The parent's environment with three keys forced, rather than a
            # bare one: a stripped environment is purer and is also how a run
            # breaks on a machine whose interpreter needs something in it.
            # PYTHONHASHSEED is the only variable this test is actually about.
            env={
                **os.environ,
                "DJANGO_SETTINGS_MODULE": "tests.conftest_settings",
                "PYTHONHASHSEED": seed,
                "PYTHONPATH": str(_ROOT),
            },
        ).stdout.strip()
        for seed in ("0", "424242")
    }

    assert len(answers) == 1
    # And the same answer this process gives, so the two subprocesses agreeing
    # with each other but not with the suite cannot pass either.
    assert answers == {
        shape_digest(
            Shape(
                Table(
                    Order,
                    rows=17,
                    status=Skew({"a": 0.9, "b": 0.1}),
                    total=Constant("1.50"),
                    created_at=Sequential(_AWARE, datetime.timedelta(seconds=3)),
                ),
                seed=5,
            )
        )
    }


def test_the_seed_moves_it() -> None:
    assert shape_digest(_orders(seed=1)) != shape_digest(_orders(seed=2))


def test_the_row_count_moves_it() -> None:
    assert shape_digest(_orders(rows=100)) != shape_digest(_orders(rows=101))


def test_the_order_of_a_skews_weights_moves_it_because_it_moves_the_data() -> None:
    forwards = Skew({"a": 0.9, "b": 0.1})
    backwards = Skew({"b": 0.1, "a": 0.9})

    # The evidence for the digest's choice rather than an assertion about the
    # digest alone: the cumulative bounds are laid out in declaration order, so
    # one draw lands on different values. A digest that sorted the weights would
    # hand these two databases one cache key.
    assert forwards.value(0, 0.05) != backwards.value(0, 0.05)
    assert shape_digest(_orders(status=forwards)) != shape_digest(_orders(status=backwards))


def test_the_order_the_fields_were_written_in_does_not() -> None:
    # The other half of the same decision. Field order becomes a sorted COPY
    # column list before a row is generated, so it provably cannot reach the
    # data -- and hashing it would mean building the same database twice because
    # two keyword arguments were swapped.
    one = Shape(
        Table(Order, rows=5, status=Constant("a"), total=Constant(1), created_at=Constant(_AWARE))
    )
    other = Shape(
        Table(Order, rows=5, created_at=Constant(_AWARE), total=Constant(1), status=Constant("a"))
    )

    assert shape_digest(one) == shape_digest(other)


def test_the_order_the_tables_were_written_in_does_move_it() -> None:
    # Unlike fields, and for a reason: a raw projection names nothing it reads,
    # so several of them are ordered after everything and fall back to
    # declaration order. That can reach the data, so the safe answer is a
    # different key and a second build.
    company = Table(Company, rows=3, name=Constant("acme"))
    account = Table(Account, rows=3, signed_up_at=Constant(_AWARE), plan=Constant("free"))

    assert shape_digest(Shape(company, account)) != shape_digest(Shape(account, company))


@pytest.mark.parametrize(
    ("one", "other"),
    [
        pytest.param(Constant("1"), Constant(1), id="a string is not an integer"),
        pytest.param(Constant(1), Constant(True), id="an integer is not a boolean"),
        pytest.param(Constant(1), Constant(Decimal("1")), id="an integer is not a decimal"),
        pytest.param(Constant(1), Constant(1.0), id="an integer is not a float"),
        pytest.param(Constant(None), Constant("None"), id="nothing is not the word"),
        pytest.param(Constant(("ab",)), Constant(("a", "b")), id="one item is not two"),
        pytest.param(
            Constant({"a": "sb"}),
            Constant({"as": "b"}),
            # The pair that pins the length prefix rather than merely benefiting
            # from it. Every other case here is separated by its kind byte, so a
            # digest that dropped the lengths still told them apart; these two
            # differ only in where the boundary falls, and the kind byte of the
            # second string is absorbable into the first. Without the length
            # this is one byte stream and two databases.
            id="a boundary that would otherwise shift",
        ),
        pytest.param(Constant(("a",)), Constant({"a": None}), id="a sequence is not a mapping"),
        pytest.param(Constant(_Plan.FREE), Constant("free"), id="an enum is not its value"),
        pytest.param(Constant(b"a"), Constant("a"), id="bytes are not text"),
        pytest.param(Constant(0.1), Constant(0.2), id="two floats"),
        pytest.param(
            Constant(datetime.date(2020, 1, 1)),
            Constant(datetime.date(2020, 1, 2)),
            id="two dates",
        ),
        pytest.param(Constant(datetime.time(1, 0)), Constant(datetime.time(2, 0)), id="two times"),
        pytest.param(
            Constant(_AWARE),
            Constant(_AWARE.replace(tzinfo=None)),
            id="aware is not naive",
        ),
        pytest.param(
            Constant(datetime.timedelta(days=1)),
            Constant(datetime.timedelta(days=1, microseconds=1)),
            id="two timedeltas",
        ),
        pytest.param(Constant(uuid.UUID(int=1)), Constant(uuid.UUID(int=2)), id="two uuids"),
        pytest.param(Uniform(0, 1), Uniform(0, 2), id="two uniforms"),
        pytest.param(Uniform(0, 1), Uniform(0, 1, places=2), id="rounding"),
        pytest.param(Skew({"a": 1}), Skew({"a": 2}), id="two weights"),
        pytest.param(Sequential(0, 1), Sequential(0, 2), id="two steps"),
    ],
)
def test_every_kind_of_value_reaches_the_digest(one: object, other: object) -> None:
    # One parametrisation rather than a test per leaf type, because the claim is
    # the same claim every time: a value this package can read has to change the
    # answer, or a declaration could be edited without the cache noticing.
    assert shape_digest(_orders(total=one)) != shape_digest(_orders(total=other))


def test_the_same_value_twice_is_the_same_digest() -> None:
    # The other direction, and it is not implied by the test above: an encoding
    # that mixed in something incidental -- an object's address, an interpreter
    # counter -- would pass every inequality test in this module and never hit
    # the cache.
    assert shape_digest(_orders(total=Constant(Decimal("1.50")))) == shape_digest(
        _orders(total=Constant(Decimal("1.50")))
    )


@pytest.mark.parametrize(
    ("one", "other"),
    [
        pytest.param(FanOut(Zipf()), FanOut(Zipf(1.5)), id="the exponent"),
        pytest.param(FanOut(Zipf()), FanOut(Uniform(1, 10)), id="the distribution"),
        pytest.param(FanOut(Zipf()), FanOut(Zipf(), childless=0.3), id="the childless share"),
        pytest.param(FanOut(Zipf()), FanOut(Zipf(), null=0.3), id="the null share"),
        pytest.param(
            FanOut(Zipf()), FanOut(Zipf(), placement="grouped"), id="the physical placement"
        ),
    ],
)
def test_every_part_of_a_fan_out_reaches_the_digest(one: FanOut, other: FanOut) -> None:
    def shape(relation: FanOut) -> Shape:
        return Shape(
            Table(Company, rows=10, name=Constant("acme")),
            Table(Session, rows=100, label=Constant("s"), company=relation),
        )

    assert shape_digest(shape(one)) != shape_digest(shape(other))


@pytest.mark.parametrize(
    ("one", "other"),
    [
        pytest.param(
            After("account.signed_up_at", within=datetime.timedelta(days=1)),
            After("account.signed_up_at", within=datetime.timedelta(days=2)),
            id="the window",
        ),
        pytest.param(
            After("account.signed_up_at", within=datetime.timedelta(days=1)),
            After(
                "account.signed_up_at",
                within=datetime.timedelta(days=1),
                at_least=datetime.timedelta(hours=1),
            ),
            id="the minimum gap",
        ),
    ],
)
def test_a_derivation_that_is_data_reaches_the_digest(one: object, other: object) -> None:
    def shape(opened_at: object) -> Shape:
        return Shape(
            Table(Account, rows=5, signed_up_at=Constant(_AWARE), plan=Constant("free")),
            Table(
                Ticket,
                rows=20,
                fields={
                    "account": FanOut(Zipf()),
                    "opened_at": opened_at,
                    "severity": Constant("low"),
                    "quantity": Constant(1),
                    "unit_price": Constant(1),
                    "total": Constant(1),
                },
            ),
        )

    assert shape_digest(shape(one)) != shape_digest(shape(other))


def test_a_conditional_distribution_carries_its_cases_and_its_default() -> None:
    def shape(severity: object) -> Shape:
        return Shape(
            Table(Account, rows=5, signed_up_at=Constant(_AWARE), plan=Constant("free")),
            Table(
                Ticket,
                rows=20,
                fields={
                    "account": FanOut(Zipf()),
                    "opened_at": Constant(_AWARE),
                    "severity": severity,
                    "quantity": Constant(1),
                    "unit_price": Constant(1),
                    "total": Constant(1),
                },
            ),
        )

    cases = Given("account.plan", {"free": Skew({"low": 1})})
    other_cases = Given("account.plan", {"free": Skew({"high": 1})})
    with_default = Given("account.plan", {"free": Skew({"low": 1})}, default=Skew({"low": 1}))

    assert shape_digest(shape(cases)) != shape_digest(shape(other_cases))
    assert shape_digest(shape(cases)) != shape_digest(shape(with_default))


def test_a_shared_rank_and_its_direction_reach_the_digest() -> None:
    def shape(total: object) -> Shape:
        return Shape(
            Table(Account, rows=5, signed_up_at=Constant(_AWARE), plan=Constant("free")),
            Table(
                Ticket,
                rows=20,
                fields={
                    "account": FanOut(Zipf()),
                    "opened_at": Constant(_AWARE),
                    "severity": Constant("low"),
                    "quantity": Constant(1),
                    "unit_price": Constant(1),
                    "total": total,
                },
            ),
        )

    forwards = Aligned("size", Uniform(0, 10))
    backwards = Aligned("size", Uniform(0, 10), reverse=True)
    renamed = Aligned("bulk", Uniform(0, 10))

    assert shape_digest(shape(forwards)) != shape_digest(shape(backwards))
    assert shape_digest(shape(forwards)) != shape_digest(shape(renamed))


def test_the_key_strategy_reaches_the_digest() -> None:
    # Two builds with different key strategies produce different primary keys
    # and therefore different foreign keys everywhere. A digest that ignored the
    # strategy would be the clearest possible way to serve the wrong database.
    inferred = Shape(Table(Company, rows=5, name=Constant("acme")))
    explicit = Shape(Table(Company, rows=5, name=Constant("acme"), keys=SequentialKeys()))
    other = Shape(Table(Company, rows=5, name=Constant("acme"), keys=UuidKeys()))

    assert shape_digest(inferred) == shape_digest(explicit)
    assert shape_digest(inferred) != shape_digest(other)


def test_a_statistics_target_reaches_the_digest() -> None:
    # It changes what the planner records, which is the only thing this package
    # claims to change at all. A template built without one is not the database
    # a declaration asking for one describes.
    assert shape_digest(_orders()) != shape_digest(
        Shape(
            Table(
                Order,
                rows=100,
                fields={
                    "status": Skew({"complete": 0.98, "pending": 0.02}),
                    "total": Uniform(0, 500, places=2),
                    "created_at": Sequential(_AWARE, datetime.timedelta(seconds=3)),
                },
                statistics={"status": 400},
            )
        )
    )


def test_a_projections_derived_statement_reaches_the_digest() -> None:
    def shape(projection: Projection) -> Shape:
        return Shape(
            Table(Template, rows=3, name=Constant("t")),
            Table(
                TemplateSession,
                rows=9,
                template=FanOut(Zipf()),
                title=Constant("s"),
                minutes=Constant(1),
            ),
            Table(Event, rows=6, template=FanOut(Zipf()), name=Constant("e")),
            projection,
        )

    derived = Projection(EventSession, per=Event, copying=TemplateSession)
    written = Projection(
        EventSession,
        columns=("id", "event", "title", "minutes"),
        sql="SELECT 1, e.id, t.title, t.minutes FROM event e JOIN templatesession t ON true",
    )
    other_sql = Projection(
        EventSession,
        columns=("id", "event", "title", "minutes"),
        sql="SELECT 2, e.id, t.title, t.minutes FROM event e JOIN templatesession t ON true",
    )

    assert shape_digest(shape(derived)) != shape_digest(shape(written))
    assert shape_digest(shape(written)) != shape_digest(shape(other_sql))


def test_a_projections_parameters_reach_the_digest() -> None:
    def shape(params: tuple[object, ...]) -> Shape:
        return Shape(
            Table(Event, rows=6, template=FanOut(Zipf()), name=Constant("e")),
            Table(Template, rows=3, name=Constant("t")),
            Projection(
                EventSession,
                columns=("id", "event", "title", "minutes"),
                sql="SELECT 1, e.id, %s, 1 FROM event e",
                params=params,
            ),
        )

    assert shape_digest(shape(("one",))) != shape_digest(shape(("two",)))


def test_a_derivation_wrapping_a_callable_is_refused_by_name() -> None:
    # The decision this module exists to record. There is no honest digest of a
    # function: two lambdas share a name, and identical bytecode returns
    # something else when a constant it reads is edited elsewhere. Every one of
    # those failures agrees while the data has changed, which is the direction a
    # cache key must never be wrong in.
    shape = Shape(
        Table(
            Order,
            rows=5,
            status=Constant("a"),
            total=Constant(1),
            created_at=Constant(_AWARE),
            note=Derived("status", compute=str),
        )
    )

    with pytest.raises(UnhashableShape) as raised:
        shape_digest(shape)

    message = str(raised.value)
    # It has to name where, or a reader is told a whole shape is unhashable and
    # left to find which column did it.
    assert "'note'" in message
    assert Order._meta.db_table in message
    assert "Derived" in message


def test_a_key_function_is_refused_by_name() -> None:
    shape = Shape(Table(SlugPk, rows=5, name=Constant("x"), keys=KeyFunction(lambda row: str(row))))

    with pytest.raises(UnhashableShape, match="KeyFunction"):
        shape_digest(shape)


def test_a_value_this_package_cannot_read_is_refused_by_name() -> None:
    # The same rule one level down. A Constant holding an object with no
    # canonical form is a value that decides a column, and leaving it out of the
    # key would mean two different tables sharing a cached database.
    shape = _orders(total=Constant(object()))

    with pytest.raises(UnhashableShape) as raised:
        shape_digest(shape)

    assert "'total'" in str(raised.value)


class _CountingDistribution:
    """A consumer's own distribution that really is data, and says so."""

    def __init__(self, step: int) -> None:
        self._step = step

    def value(self, row: int, draw: float) -> object:
        return row * self._step

    def canonical(self) -> object:
        return (self._step,)


def test_a_consumers_own_declaration_can_join_in() -> None:
    # The way out of the refusal, and the reason it is a protocol rather than a
    # list of this package's own classes: a distribution written elsewhere that
    # is genuinely a function of its parameters can say so, and then a shape
    # using it caches like any other.
    assert shape_digest(_orders(total=_CountingDistribution(1))) == shape_digest(
        _orders(total=_CountingDistribution(1))
    )
    assert shape_digest(_orders(total=_CountingDistribution(1))) != shape_digest(
        _orders(total=_CountingDistribution(2))
    )


def test_a_declaration_that_merely_describes_itself_the_same_way_is_still_distinct() -> None:
    # The type's own name is in the digest, so a caller's one-parameter
    # distribution and this package's cannot collide by describing themselves
    # with the same parts.
    assert shape_digest(_orders(total=_CountingDistribution(3))) != shape_digest(
        _orders(total=Zipf(3))
    )


def test_no_two_of_the_shapes_in_this_module_share_a_key() -> None:
    # A sweep rather than another pair, because collision is the failure that
    # matters and pairwise tests only rule out the pairs somebody thought of.
    shapes = [
        _orders(),
        _orders(rows=101),
        _orders(seed=1),
        _orders(total=Constant(1)),
        _orders(total=Constant("1")),
        _orders(status=Skew({"a": 1, "b": 1})),
        _orders(status=Skew({"b": 1, "a": 1})),
        Shape(Table(Company, rows=5, name=Constant("acme"))),
        Shape(Table(Company, rows=5, name=Constant("acme")), seed=1),
    ]

    assert len({shape_digest(shape) for shape in shapes}) == len(shapes)
