"""Build once per machine, clone per session, and never serve a stale database."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest
from django.db import connection, connections, transaction
from django.db.transaction import TransactionManagementError
from django.test import override_settings

from django_data_shape import (
    Constant,
    FanOut,
    InvalidShape,
    KeyFunction,
    Projection,
    Shape,
    Skew,
    Table,
    UnhashableShape,
    Uniform,
    UnsupportedBackend,
    Zipf,
    clone_database,
    drop_database,
    template_database,
)
from django_data_shape.template_database import PREFIX, _context, _key, _schema_digest
from django_data_shape.version import __version__
from tests.testapp.models import Catalogue, Event, EventSession, SlugPk, Template, TemplateSession

# transaction=True throughout, and it is a requirement rather than a habit here:
# filling a template means pointing the connection at another database and
# closing it, which cannot be done inside the atomic block a plain django_db
# test wraps everything in. That refusal has a test of its own below.
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="CREATE DATABASE ... TEMPLATE has no equivalent on another backend",
    ),
]


def _shape(rows: int = 40, seed: int = 0) -> Shape:
    return Shape(Table(Catalogue, rows=rows, name=Skew({"widget": 3, "cog": 1})), seed=seed)


@pytest.fixture
def temporary_databases() -> Iterator[list[str]]:
    """Every database a test makes, dropped afterwards.

    Templates are deliberately never cleaned up by the package -- a cache keyed
    by content has nothing to garbage-collect against -- so a suite that makes
    them has to. Without this the machine accumulates one per shape per code
    change, which is a slow leak and an unpleasant thing to discover.
    """
    made: list[str] = []
    yield made
    for name in reversed(made):
        drop_database(name)


def _template(shape: Shape, made: list[str]) -> str:
    name = template_database(shape)
    made.append(name)
    return name


def _rows_in(database: str, statement: str) -> list[tuple[object, ...]]:
    """Read from a cloned database directly, with no Django connection involved.

    psycopg rather than a second Django alias, because the thing being checked
    is what landed in a database this package created outside Django's own
    settings -- and reading it through a connection Django configured would be
    reading the settings back rather than the database.
    """
    settings = connections["default"].settings_dict
    with (
        psycopg.connect(
            dbname=database,
            host=settings["HOST"] or None,
            port=settings["PORT"] or None,
            user=settings["USER"] or None,
            password=settings["PASSWORD"] or None,
        ) as opened,
        opened.cursor() as cursor,
    ):
        cursor.execute(statement)
        return cursor.fetchall()


def _oid(database: str) -> int | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT oid FROM pg_database WHERE datname = %s", [database])
        row = cursor.fetchone()
    return None if row is None else int(row[0])


def test_it_creates_a_template_named_after_the_declaration(
    temporary_databases: list[str],
) -> None:
    name = _template(_shape(), temporary_databases)

    assert name.startswith(PREFIX)
    assert _oid(name) is not None


def test_asking_twice_does_not_build_twice(temporary_databases: list[str]) -> None:
    shape = _shape()
    name = _template(shape, temporary_databases)
    first = _oid(name)

    again = template_database(shape)

    # The object identity of the database itself, which is the only thing that
    # can tell a reuse from a rebuild: a second build would have created another
    # database under the same name and it would carry a new oid. Row counts
    # cannot tell them apart, because a rebuild produces exactly the same rows.
    assert again == name
    assert _oid(again) == first


def test_two_shapes_do_not_share_a_template(temporary_databases: list[str]) -> None:
    assert _template(_shape(rows=40), temporary_databases) != _template(
        _shape(rows=41), temporary_databases
    )


def test_the_seed_alone_is_enough_to_make_it_a_different_database(
    temporary_databases: list[str],
) -> None:
    # Two shapes with the same cardinality and different rows in it. A key that
    # missed this would hand a suite a database whose every value was decided by
    # a seed nobody asked for.
    assert _template(_shape(seed=1), temporary_databases) != _template(
        _shape(seed=2), temporary_databases
    )


def test_nothing_may_connect_to_a_finished_template(temporary_databases: list[str]) -> None:
    # Not tidiness: the one failure mode of this whole mechanism is PostgreSQL
    # refusing to copy a database something is attached to. Turning connections
    # off is what makes that impossible rather than unlikely.
    name = _template(_shape(), temporary_databases)

    with connection.cursor() as cursor:
        cursor.execute("SELECT datallowconn FROM pg_database WHERE datname = %s", [name])

        assert cursor.fetchone()[0] is False


def test_a_clone_carries_the_rows(temporary_databases: list[str]) -> None:
    name = _template(_shape(rows=40), temporary_databases)
    target = f"{name}_clone"
    temporary_databases.append(target)

    clone_database(name, target)

    assert _rows_in(target, f"SELECT count(*) FROM {Catalogue._meta.db_table}") == [(40,)]


def test_and_the_statistics_with_them(temporary_databases: list[str]) -> None:
    # The measurement the whole design rests on, asserted rather than assumed:
    # if the planner had to be shown the rows again after every clone, the clone
    # would cost an ANALYZE per session and the ratio this package sells would
    # not exist. pg_statistic is ordinary catalogue content, so it comes along.
    name = _template(_shape(rows=400), temporary_databases)
    target = f"{name}_clone"
    temporary_databases.append(target)

    clone_database(name, target)

    values = _rows_in(
        target,
        "SELECT most_common_vals FROM pg_stats "
        f"WHERE tablename = '{Catalogue._meta.db_table}' AND attname = 'name'",
    )
    assert values != []
    assert "widget" in values[0][0]


def test_a_clone_can_take_the_servers_own_strategy(temporary_databases: list[str]) -> None:
    # The path an older PostgreSQL has, and the one the strategy gate points at.
    # It produces the same database, more slowly.
    name = _template(_shape(rows=40), temporary_databases)
    target = f"{name}_wal"
    temporary_databases.append(target)

    clone_database(name, target, strategy=None)

    assert _rows_in(target, f"SELECT count(*) FROM {Catalogue._meta.db_table}") == [(40,)]


def test_a_clone_will_not_overwrite_unless_it_is_told_to(
    temporary_databases: list[str],
) -> None:
    name = _template(_shape(rows=40), temporary_databases)
    target = f"{name}_twice"
    temporary_databases.append(target)
    clone_database(name, target)

    # The default destroys nothing, so a second session that forgot to clean up
    # gets an error naming the database rather than losing it.
    with pytest.raises(Exception, match="already exists"):
        clone_database(name, target)

    clone_database(name, target, replace=True)

    assert _rows_in(target, f"SELECT count(*) FROM {Catalogue._meta.db_table}") == [(40,)]


def test_a_shape_whose_build_fails_leaves_no_half_built_template() -> None:
    # A projection over tables nobody filled inserts nothing, which build()
    # refuses. What matters here is what is left behind: the database is created
    # under a working name and only renamed once the build succeeds, so a
    # failure leaves nothing that could ever be mistaken for a finished
    # template.
    shape = Shape(Projection(EventSession, per=Event, copying=TemplateSession))

    with pytest.raises(InvalidShape, match="inserted no rows"):
        template_database(shape)

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_database WHERE datname LIKE %s", [f"{PREFIX}%\\_\\_partial"]
        )

        assert cursor.fetchone()[0] == 0


def test_a_template_holds_a_whole_graph(temporary_databases: list[str]) -> None:
    # One table proves the mechanism; the graph proves the mechanism is applied
    # to the thing this package is actually for. The migration ran, every
    # declared table was filled in dependency order, and the projection found
    # its inputs already there.
    shape = Shape(
        Table(Template, rows=5, name=Constant("t")),
        Table(
            TemplateSession,
            rows=20,
            template=FanOut(Zipf()),
            title=Constant("s"),
            minutes=Constant(1),
        ),
        Table(Event, rows=30, template=FanOut(Zipf()), name=Constant("e")),
        Projection(EventSession, per=Event, copying=TemplateSession),
        seed=11,
    )
    name = _template(shape, temporary_databases)
    target = f"{name}_graph"
    temporary_databases.append(target)

    clone_database(name, target)

    assert _rows_in(target, f"SELECT count(*) FROM {Event._meta.db_table}") == [(30,)]
    assert _rows_in(target, f"SELECT count(*) FROM {EventSession._meta.db_table}")[0][0] > 0


def test_a_statistics_target_survives_the_clone(temporary_databases: list[str]) -> None:
    # The two halves of this release meeting: a target is catalogue state like
    # the statistics themselves, so a cloned database is planner-ready in the
    # way the declaration asked for rather than merely in the default way.
    shape = Shape(
        Table(
            Catalogue,
            rows=200,
            name=Skew({f"n{index}": 1.0 for index in range(20)}),
            statistics={"name": 314},
        ),
        seed=12,
    )
    name = _template(shape, temporary_databases)
    target = f"{name}_target"
    temporary_databases.append(target)

    clone_database(name, target)

    assert _rows_in(
        target,
        "SELECT attstattarget FROM pg_attribute "
        f"WHERE attrelid = '{Catalogue._meta.db_table}'::regclass AND attname = 'name'",
    ) == [(314,)]


def test_a_shape_that_cannot_be_hashed_is_refused_before_anything_is_created() -> None:
    # The refusal that keeps the cache honest, met from the direction a consumer
    # meets it: a template is asked for and the shape says it cannot be
    # recognised twice. Nothing is created, because the name cannot be computed
    # at all.
    shape = Shape(Table(SlugPk, rows=5, name=Constant("x"), keys=KeyFunction(lambda row: str(row))))

    with pytest.raises(UnhashableShape, match="KeyFunction"):
        template_database(shape)


def test_it_refuses_to_run_inside_a_transaction() -> None:
    # Rather than poisoning the connection. Django marks a connection closed
    # inside an atomic block as unusable for the rest of that block, so the
    # failure without this guard is not here but in whatever ran next.
    with transaction.atomic(), pytest.raises(TransactionManagementError, match="atomic block"):
        template_database(_shape())


def test_dropping_says_whether_there_was_anything_to_drop(
    temporary_databases: list[str],
) -> None:
    name = _template(_shape(), temporary_databases)

    assert drop_database(name) is True
    # Twice, because the answer is what a caller cleaning up reads, and an
    # unconditional True would make it useless.
    assert drop_database(name) is False


@pytest.mark.django_db(transaction=True, databases=["default", "not_postgres"])
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: template_database(_shape(), using="not_postgres"), id="caching"),
        pytest.param(lambda: clone_database("a", "b", using="not_postgres"), id="cloning"),
        pytest.param(lambda: drop_database("a", using="not_postgres"), id="dropping"),
    ],
)
def test_none_of_it_is_offered_on_a_backend_that_has_no_such_statement(call: object) -> None:
    # Driven through the real entry points against a real non-PostgreSQL alias,
    # which is the difference between asserting a guard exists and falsifying
    # its absence: a stub proves the gate works, not that anything calls it.
    with pytest.raises(UnsupportedBackend):
        call()


def test_the_declaration_reaches_the_database_and_not_only_the_name(
    temporary_databases: list[str],
) -> None:
    # The end of the chain, and the claim a cache is worth having at all: what
    # comes out of a clone is the database the declaration describes -- the row
    # count, the skew and the keys -- rather than a database with the right name.
    shape = Shape(
        Table(
            Catalogue,
            rows=500,
            name=Skew({"widget": 0.9, "cog": 0.1}),
        ),
        seed=13,
    )
    name = _template(shape, temporary_databases)
    target = f"{name}_end"
    temporary_databases.append(target)

    clone_database(name, target)

    table = Catalogue._meta.db_table
    assert _rows_in(target, f"SELECT count(*) FROM {table} WHERE name = 'widget'")[0][0] > 400
    assert _rows_in(target, f"SELECT min(id), max(id) FROM {table}") == [(1, 500)]


def test_a_declaration_the_cache_cannot_key_on_is_the_only_thing_it_refuses(
    temporary_databases: list[str],
) -> None:
    # A continuous distribution has no distinct-value count and a fan-out is not
    # a value distribution at all, and neither stops a shape being hashed: the
    # refusal is about callables, not about anything a declaration ordinarily
    # holds.
    shape = Shape(
        Table(Template, rows=4, name=Constant("t")),
        Table(
            TemplateSession,
            rows=16,
            template=FanOut(Uniform(1, 5)),
            title=Constant("s"),
            minutes=Constant(1),
        ),
        seed=14,
    )

    assert _template(shape, temporary_databases).startswith(PREFIX)


# The key, taken apart. Each piece is a function of its arguments rather than of
# the world, which is what makes "the schema reaches the cache key" a claim a
# test can falsify instead of a sentence in a docstring.


def test_the_declaration_reaches_the_key() -> None:
    context = ("version", "schema", "True", "UTC")

    assert _key(_shape(rows=1), context) != _key(_shape(rows=2), context)


def test_and_so_does_everything_around_it() -> None:
    # The composition, checked separately from the parts: a key that stopped
    # mixing the context in would still look exactly like this one from outside,
    # and would serve a database built by an older release or into an older
    # schema.
    shape = _shape()

    assert _key(shape, ("version", "schema", "True", "UTC")) != _key(
        shape, ("version", "different schema", "True", "UTC")
    )


def test_the_schema_digest_moves_when_a_model_does() -> None:
    # An app with no migrations has its tables built straight from the models,
    # so a column added or renamed there changes the database while every
    # migration name stays the same.
    assert _schema_digest((Catalogue,), ()) != _schema_digest((Catalogue, Event), ())


def test_and_when_a_migration_is_added() -> None:
    # The other half. A migration that adds an index or a constraint changes the
    # database while leaving every model field exactly as it was.
    assert _schema_digest((Catalogue,), ()) != _schema_digest(
        (Catalogue,), (("testapp", "0001_initial"),)
    )


def test_the_package_version_is_part_of_the_context() -> None:
    # A release that changes how a distribution draws changes the rows without
    # changing a word of the declaration, so a cache keyed on the declaration
    # alone would hand the new code the old database.
    assert __version__ in _context()


@pytest.mark.parametrize(
    "settings_override",
    [
        pytest.param({"TIME_ZONE": "America/New_York"}, id="the time zone"),
        pytest.param({"USE_TZ": False}, id="whether time zones are used at all"),
    ],
)
def test_the_settings_that_decide_what_a_datetime_holds_are_too(
    settings_override: dict[str, object],
) -> None:
    # Every value goes through its field's get_db_prep_save on the way into COPY,
    # so a datetime column lands somewhere else under a different setting. Same
    # declaration, different database, and therefore a different key.
    before = _context()

    with override_settings(**settings_override):
        assert _context() != before
