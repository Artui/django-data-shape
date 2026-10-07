"""Build once per machine, clone per session, and never serve a stale database."""

from __future__ import annotations

import secrets
from collections.abc import Iterator

import django
import psycopg
import pytest
from django.apps import apps
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import connection, connections, migrations, transaction
from django.db.migrations import Migration
from django.db.migrations.loader import MigrationLoader
from django.db.transaction import TransactionManagementError
from django.test import override_settings

from django_data_shape import (
    Constant,
    FanOut,
    InvalidShape,
    KeyFunction,
    Projection,
    Shape,
    ShapeNotEmpty,
    Skew,
    Table,
    UnhashableShape,
    Uniform,
    UnsupportedBackend,
    UnusableBase,
    Zipf,
    clone_database,
    drop_database,
    template_database,
)
from django_data_shape.databases.template_database import (
    PREFIX,
    _ahead_of_disk,
    _base_context,
    _context,
    _key,
    _schema_digest,
    _stranded_squashes,
)
from django_data_shape.version import __version__
from tests.testapp.models import (
    Award,
    Catalogue,
    Company,
    Event,
    EventSession,
    SlugPk,
    Subscriber,
    Template,
    TemplateSession,
    Wearer,
)

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


def _execute_in(database: str, statement: str) -> None:
    """Write to a database directly, as an operator editing a base by hand would."""
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


def _base(made: list[str]) -> str:
    """A migrated database to start templates from, dropped with everything else.

    A copy of the test database, which pytest-django has already migrated --
    the same thing a project keeping a base for its own test databases has, and
    a copy rather than a second migrate because it is the migrated state that
    matters here and not the minutes it took to reach it. Not named with the
    package's prefix, so that it is never counted as a template.
    """
    name = f"shape_base_{secrets.token_hex(4)}"
    source = connection.settings_dict["NAME"]
    quote = connection.ops.quote_name
    # PostgreSQL refuses to copy a database anything is attached to, and the
    # test's own connection is.
    connection.close()
    with connection._nodb_cursor() as cursor:
        cursor.execute(f"CREATE DATABASE {quote(name)} TEMPLATE {quote(source)}")
    made.append(name)
    return name


def _migrate(database: str, *arguments: str) -> None:
    """Run ``migrate`` against ``database``, as a project keeping a base would.

    The connection is pointed at it the way the package points one at a
    partial, and closed on both sides, because PostgreSQL refuses to copy a
    database anything is attached to and the next step is always a copy.
    """
    original = connection.settings_dict["NAME"]
    connection.close()
    connection.settings_dict["NAME"] = database
    try:
        call_command("migrate", *arguments, interactive=False, verbosity=0)
    finally:
        connection.close()
        connection.settings_dict["NAME"] = original


def _templates_on_the_server() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT datname FROM pg_database WHERE datname LIKE %s", [f"{PREFIX}%"])
        return {row[0] for row in cursor.fetchall()}


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


def test_a_shape_whose_build_fails_leaves_no_half_built_template(
    temporary_databases: list[str],
) -> None:
    # A projection over tables nobody filled inserts nothing, which build()
    # refuses. What matters here is what is left behind: the database is created
    # under a working name and only renamed once the build succeeds, so a
    # failure leaves nothing that could ever be mistaken for a finished
    # template.
    shape = Shape(Projection(EventSession, per=Event, copying=TemplateSession))

    with pytest.raises(InvalidShape, match="inserted no rows"):
        temporary_databases.append(template_database(shape))

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


def test_a_shape_that_cannot_be_hashed_is_refused_before_anything_is_created(
    temporary_databases: list[str],
) -> None:
    # The refusal that keeps the cache honest, met from the direction a consumer
    # meets it: a template is asked for and the shape says it cannot be
    # recognised twice. Nothing is created, because the name cannot be computed
    # at all.
    shape = Shape(Table(SlugPk, rows=5, name=Constant("x"), keys=KeyFunction(lambda row: str(row))))

    with pytest.raises(UnhashableShape, match="KeyFunction"):
        temporary_databases.append(template_database(shape))


def test_it_refuses_to_run_inside_a_transaction(temporary_databases: list[str]) -> None:
    # Rather than poisoning the connection. Django marks a connection closed
    # inside an atomic block as unusable for the rest of that block, so the
    # failure without this guard is not here but in whatever ran next.
    with transaction.atomic(), pytest.raises(TransactionManagementError, match="atomic block"):
        temporary_databases.append(template_database(_shape()))


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
    context = ("version", "schema", "True", "UTC")

    assert _key(shape, context) != _key(shape, ("version", "different schema", "True", "UTC"))
    # And a base, when there is one: its name and its database oid follow the
    # rest of the context. The oid is the part doing the work -- a base dropped
    # and recreated under the same name, which is how one restored from a dump
    # is usually refreshed, is a new oid and so a new key.
    keys = {
        _key(shape, context),
        _key(shape, (*context, "base", "16384")),
        _key(shape, (*context, "base", "16385")),
        _key(shape, (*context, "other", "16384")),
    }
    assert len(keys) == 4


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


# Starting from a migrated base. A project with a long migration history pays
# for the replay on every template it builds from empty, and usually already
# keeps a migrated database for exactly that reason.


def test_a_template_can_start_from_a_migrated_base(temporary_databases: list[str]) -> None:
    base = _base(temporary_databases)
    # A row in a table the shape does not declare, so that the template can be
    # told apart from one built from empty: nothing else would put it there.
    _execute_in(base, "INSERT INTO auth_group (name) VALUES ('from the base')")

    name = template_database(_shape(rows=40), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert name.startswith(PREFIX)
    assert _rows_in(target, "SELECT name FROM auth_group") == [("from the base",)]
    assert _rows_in(target, f"SELECT count(*) FROM {Catalogue._meta.db_table}") == [(40,)]


def test_migrate_still_runs_over_the_copy(temporary_databases: list[str]) -> None:
    # Finding no migration to apply is not the same as having nothing to do: an
    # app with no migrations gets its tables from run_syncdb, and a base made by
    # a plain migrate does not have them. The test app is such an app, so a
    # template that skipped migrate because it started from a base would have
    # no table to build the shape into.
    base = f"shape_base_{secrets.token_hex(4)}"
    with connection._nodb_cursor() as cursor:
        cursor.execute(f"CREATE DATABASE {connection.ops.quote_name(base)}")
    temporary_databases.append(base)
    _migrate(base)
    assert _rows_in(base, f"SELECT to_regclass('{Catalogue._meta.db_table}') IS NULL") == [(True,)]

    name = template_database(_shape(rows=40, seed=24), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert _rows_in(target, f"SELECT count(*) FROM {Catalogue._meta.db_table}") == [(40,)]


def test_a_base_is_part_of_the_key(temporary_databases: list[str]) -> None:
    # The same declaration from empty and from a base is two databases -- the
    # base's rows are template content -- so it has to be two names.
    shape = _shape(rows=40, seed=21)
    base = _base(temporary_databases)

    from_empty = _template(shape, temporary_databases)
    from_base = template_database(shape, base=base)
    temporary_databases.append(from_base)

    assert from_empty != from_base


def test_recreating_the_base_is_a_new_template(temporary_databases: list[str]) -> None:
    # The way a base restored from a schema dump is refreshed: drop it, make it
    # again under the same name. What it holds may have changed and nothing on
    # disk would say so, which is what keying on its oid answers.
    shape = _shape(rows=40, seed=22)
    base = _base(temporary_databases)
    first = template_database(shape, base=base)
    temporary_databases.append(first)

    drop_database(base)
    with connection._nodb_cursor() as cursor:
        cursor.execute(
            f"CREATE DATABASE {connection.ops.quote_name(base)} "
            f"TEMPLATE {connection.ops.quote_name(connection.settings_dict['NAME'])}"
        )
    again = template_database(shape, base=base)
    temporary_databases.append(again)

    assert again != first


def test_what_the_key_absorbs_for_a_base_is_its_name_and_its_oid(
    temporary_databases: list[str],
) -> None:
    base = _base(temporary_databases)

    assert _base_context(connection, base) == (base, str(_oid(base)))


# How wide auth_user.first_name is: 30 before auth's 0012 migration, 150 after
# it, so a schema that tells a base migrated back from one migrated forward.
_FIRST_NAME_WIDTH = (
    "SELECT character_maximum_length FROM information_schema.columns "
    "WHERE table_name = 'auth_user' AND column_name = 'first_name'"
)


def test_a_base_behind_the_migrations_on_disk_is_migrated_forward(
    temporary_databases: list[str],
) -> None:
    # The base a consumer has most often: every migration a branch adds leaves
    # a kept base one behind. Migrating forward ends at this checkout's schema
    # whatever prefix of the history the base holds, which is what the key
    # already names. Behind for real -- migrated back, so the column really is
    # the narrower one -- rather than a row deleted from django_migrations over
    # a schema that still has the change, which migrate would fail to reapply.
    base = _base(temporary_databases)
    _migrate(base, "auth", "0011_update_proxy_permissions")
    assert _rows_in(base, _FIRST_NAME_WIDTH) == [(30,)]

    name = template_database(_shape(rows=40, seed=27), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert _rows_in(target, _FIRST_NAME_WIDTH) == [(150,)]
    assert _rows_in(
        target,
        "SELECT count(*) FROM django_migrations "
        "WHERE app = 'auth' AND name = '0012_alter_user_first_name_max_length'",
    ) == [(1,)]
    assert _rows_in(target, f"SELECT count(*) FROM {Catalogue._meta.db_table}") == [(40,)]
    # The copy is what was migrated; the base is left where its owner put it.
    assert _rows_in(base, _FIRST_NAME_WIDTH) == [(30,)]


def test_migrating_the_base_forward_in_place_asks_for_the_same_template(
    temporary_databases: list[str],
) -> None:
    # Why the base's applied migrations stay out of the key: catching the base
    # up leaves its oid alone, and the template the key already names is the
    # one that catching up would have given -- the copy was migrated forward to
    # the same schema when it was built.
    shape = _shape(rows=40, seed=30)
    base = _base(temporary_databases)
    _migrate(base, "auth", "0011_update_proxy_permissions")
    behind = template_database(shape, base=base)
    temporary_databases.append(behind)

    _migrate(base)
    caught_up = template_database(shape, base=base)
    temporary_databases.append(caught_up)
    target = f"{caught_up}_end"
    temporary_databases.append(target)
    clone_database(caught_up, target)

    assert _rows_in(base, _FIRST_NAME_WIDTH) == [(150,)]
    assert caught_up == behind
    # And the template under that name does hold the schema the caught-up base
    # has, which is the half of the claim the names alone cannot show.
    assert _rows_in(target, _FIRST_NAME_WIDTH) == [(150,)]


def test_an_empty_base_is_migrated_in_full(temporary_databases: list[str]) -> None:
    # The far end of behind, and not the same path as building from empty: the
    # base has no django_migrations table at all, so reading what it has
    # applied must find nothing rather than fail. It costs what building from
    # empty costs, once per key, which is the most a behind base ever pays.
    base = f"shape_base_{secrets.token_hex(4)}"
    with connection._nodb_cursor() as cursor:
        cursor.execute(f"CREATE DATABASE {connection.ops.quote_name(base)}")
    temporary_databases.append(base)

    name = template_database(_shape(rows=40, seed=29), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert _rows_in(target, _FIRST_NAME_WIDTH) == [(150,)]
    assert _rows_in(target, f"SELECT count(*) FROM {Catalogue._meta.db_table}") == [(40,)]


def test_a_base_ahead_of_the_migrations_on_disk_is_refused(temporary_databases: list[str]) -> None:
    # The one base migrating cannot fix. A base migrated by another branch
    # holds schema the migrations on disk do not describe, and nothing here can
    # take it away -- an index that branch added would build silently and skew
    # every plan assertion over the template.
    base = _base(temporary_databases)
    _execute_in(
        base,
        "INSERT INTO django_migrations (app, name, applied) "
        "VALUES ('contenttypes', '9999_ghost', now())",
    )
    before = _templates_on_the_server()

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(_shape(), base=base))

    message = str(refused.value)
    assert base in message
    assert "ahead" in message
    assert "contenttypes.9999_ghost" in message
    # Both remedies, because the two causes need opposite ones: pruning a
    # branch's rows would leave its schema behind and stop the refusal.
    assert "manage.py migrate contenttypes --prune" in message
    assert "recreate it" in message
    assert _templates_on_the_server() == before


def test_a_base_that_goes_ahead_is_refused_with_its_template_already_built(
    temporary_databases: list[str],
) -> None:
    # A base migrated in place by another branch keeps its name and its oid, so
    # the template built from it while it matched is still the one the key
    # names. The check runs before the key is looked up, so the cache hit is
    # never reached.
    base = _base(temporary_databases)
    built = template_database(_shape(rows=40, seed=25), base=base)
    temporary_databases.append(built)
    _execute_in(
        base,
        "INSERT INTO django_migrations (app, name, applied) "
        "VALUES ('contenttypes', '9999_ghost', now())",
    )

    with pytest.raises(UnusableBase, match="ahead"):
        temporary_databases.append(template_database(_shape(rows=40, seed=25), base=base))


def test_a_row_for_an_app_no_longer_installed_does_not_refuse_the_base(
    temporary_databases: list[str],
) -> None:
    # Removing an app leaves its rows in django_migrations, and they describe
    # nothing this checkout's models use: its tables, if any, are content the
    # template carries like any other table the shape does not declare.
    base = _base(temporary_databases)
    _execute_in(
        base,
        "INSERT INTO django_migrations (app, name, applied) "
        "VALUES ('removedapp', '0001_initial', now()); "
        "CREATE TABLE removedapp_thing (id int); INSERT INTO removedapp_thing VALUES (5)",
    )

    name = template_database(_shape(rows=40, seed=28), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert _rows_in(target, "SELECT id FROM removedapp_thing") == [(5,)]


def test_a_base_that_does_not_exist_is_refused_by_name(temporary_databases: list[str]) -> None:
    before = _templates_on_the_server()

    with pytest.raises(UnusableBase, match="shape_base_that_was_never_made"):
        temporary_databases.append(
            template_database(_shape(), base="shape_base_that_was_never_made")
        )

    assert _templates_on_the_server() == before


def _allow_connections(database: str, allowed: bool) -> None:
    with connection._nodb_cursor() as cursor:
        cursor.execute(
            f"ALTER DATABASE {connection.ops.quote_name(database)} "
            f"WITH ALLOW_CONNECTIONS {'true' if allowed else 'false'}"
        )


def test_a_base_that_refuses_connections_is_refused_by_name(
    temporary_databases: list[str],
) -> None:
    # Its applied migrations are read by connecting to it, so the alternative
    # is the server's own refusal surfacing as an OperationalError from inside
    # the migration loader, naming nothing a reader asked for.
    base = _base(temporary_databases)
    _allow_connections(base, False)
    before = _templates_on_the_server()

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(_shape(), base=base))

    message = str(refused.value)
    assert base in message
    assert "ALLOW_CONNECTIONS true" in message
    assert _templates_on_the_server() == before


def test_a_template_is_never_a_base(temporary_databases: list[str]) -> None:
    # The likeliest database to be refusing connections is one this package
    # made, and the remedy for that is not to allow them: a template holds a
    # shape's rows under a name keyed for that shape, so starting another from
    # it is a mistake whatever its connection setting says.
    template = _template(_shape(rows=40, seed=31), temporary_databases)

    with pytest.raises(UnusableBase, match="never a base"):
        temporary_databases.append(template_database(_shape(rows=40, seed=32), base=template))

    # Including one opened up to look inside, as the docstring tells a reader
    # how to do -- which is what holds the name check apart from the
    # connection check.
    _allow_connections(template, True)
    with pytest.raises(UnusableBase, match="never a base"):
        temporary_databases.append(template_database(_shape(rows=40, seed=32), base=template))


def test_a_partial_this_package_left_is_never_a_base(temporary_databases: list[str]) -> None:
    # A build killed between creating its working database and renaming it
    # leaves one under the template's name and the partial suffix, and it is
    # this package's as much as a finished template is.
    partial = f"{PREFIX}{secrets.token_hex(8)}__partial"
    with connection._nodb_cursor() as cursor:
        cursor.execute(f"CREATE DATABASE {connection.ops.quote_name(partial)}")
    temporary_databases.append(partial)

    with pytest.raises(UnusableBase, match="never a base"):
        temporary_databases.append(template_database(_shape(rows=40, seed=40), base=partial))


@pytest.mark.parametrize("pattern", ["data_shape_userdb_{}", "data_shape_{}"])
def test_a_database_only_named_like_a_template_is_not_refused_as_one(
    temporary_databases: list[str], pattern: str
) -> None:
    # Only a name this package generates is its template: the prefix, a digest
    # exactly as long as the key's, and the partial suffix or nothing. A user's
    # database sharing the prefix -- including one whose suffix happens to be
    # hexadecimal, but of another length -- is a base like any other, here an
    # empty one, migrated in full.
    base = pattern.format(secrets.token_hex(4))
    with connection._nodb_cursor() as cursor:
        cursor.execute(f"CREATE DATABASE {connection.ops.quote_name(base)}")
    temporary_databases.append(base)

    name = template_database(_shape(rows=40, seed=41), base=base)
    temporary_databases.append(name)

    assert _oid(name) is not None


def test_a_base_whose_name_holds_a_double_quote_is_refused(
    temporary_databases: list[str],
) -> None:
    # Django's quote_name passes a name that is already quoted through as it is
    # and does not escape an embedded quote, so a base literally named "x"
    # would be looked up as one database and copied from another. The
    # database exists and is migrated, so the name is the only thing wrong.
    quoted = f'"shape_base_{secrets.token_hex(4)}"'
    escaped = '"' + quoted.replace('"', '""') + '"'
    source = connection.settings_dict["NAME"]
    connection.close()
    with connection._nodb_cursor() as cursor:
        cursor.execute(f"CREATE DATABASE {escaped} TEMPLATE {connection.ops.quote_name(source)}")
    try:
        with pytest.raises(UnusableBase) as refused:
            temporary_databases.append(template_database(_shape(), base=quoted))
    finally:
        # By hand, because drop_database quotes the way this test is about.
        with connection._nodb_cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS {escaped}")

    message = str(refused.value)
    assert "double quote" in message
    assert "does not exist" not in message


def test_a_base_holding_rows_in_a_declared_table_is_named_as_their_cause(
    temporary_databases: list[str],
) -> None:
    # The rows are real and were put there by whoever keeps the base, so a
    # message naming only a session world -- or only "empty the table first" --
    # reads as advice about a table the test never touched. The table is one
    # of an app with migrations, because that is where a base's rows survive:
    # the tables of an app without them are rebuilt from the models.
    base = _base(temporary_databases)
    _execute_in(base, "INSERT INTO auth_group (name) VALUES ('kept in the base')")
    before = _templates_on_the_server()

    with pytest.raises(ShapeNotEmpty) as refused:
        temporary_databases.append(
            template_database(
                Shape(Table(Group, rows=1, name=Constant("declared")), seed=42), base=base
            )
        )

    message = " ".join(str(refused.value).split())
    assert message.startswith("auth_group already holds rows")
    assert (
        "a template started from a base database keeps the rows the base holds, apart from "
        "the tables it rebuilds for apps without migrations" in message
    )
    # The partial the build failed in is dropped with it, so the next run does
    # not find a half-built database under a name it would have to judge.
    assert _templates_on_the_server() == before
    assert not [name for name in _templates_on_the_server() if name.endswith("__partial")]


# Every column of a table as information_schema describes it, which is what a
# stale table differs from the model's in.
_CATALOGUE_COLUMNS = (
    "SELECT column_name, data_type, is_nullable, character_maximum_length "
    "FROM information_schema.columns "
    f"WHERE table_name = '{Catalogue._meta.db_table}' ORDER BY column_name"
)


def test_a_stale_table_of_an_app_without_migrations_is_rebuilt_from_the_model(
    temporary_databases: list[str],
) -> None:
    # run_syncdb creates a missing table and never alters an existing one, so
    # an app without migrations whose model changed after the base was made
    # kept the base's table under a key naming the new model. Its tables are
    # dropped from the copy and made again, as they are in an empty database.
    base = _base(temporary_databases)
    _execute_in(
        base,
        f"ALTER TABLE {Catalogue._meta.db_table} ALTER COLUMN name DROP NOT NULL, "
        "ADD COLUMN left_over integer",
    )
    shape = _shape(rows=40, seed=43)

    from_base = template_database(shape, base=base)
    temporary_databases.append(from_base)
    from_empty = _template(shape, temporary_databases)
    targets = [f"{from_base}_end", f"{from_empty}_end"]
    temporary_databases.extend(targets)
    clone_database(from_base, targets[0])
    clone_database(from_empty, targets[1])

    assert _rows_in(targets[0], _CATALOGUE_COLUMNS) == _rows_in(targets[1], _CATALOGUE_COLUMNS)
    assert ("name", "character varying", "NO", 50) in _rows_in(targets[0], _CATALOGUE_COLUMNS)


def test_so_a_bases_rows_in_such_a_table_do_not_carry_over(
    temporary_databases: list[str],
) -> None:
    # The other half of rebuilding: a base contributes what its migration
    # history contributes, and an app without migrations has none.
    base = _base(temporary_databases)
    _execute_in(base, f"INSERT INTO {Company._meta.db_table} (name) VALUES ('kept in the base')")

    name = template_database(_shape(rows=40, seed=44), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert _rows_in(base, f"SELECT count(*) FROM {Company._meta.db_table}") == [(1,)]
    assert _rows_in(target, f"SELECT count(*) FROM {Company._meta.db_table}") == [(0,)]


def test_an_unmanaged_models_table_in_the_base_is_carried(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # run_syncdb never makes an unmanaged model's table, so dropping one would
    # lose it rather than rebuild it: whatever made it in the base is the only
    # thing that ever will. Holds that only the tables migrate builds are
    # dropped.
    monkeypatch.setattr(Subscriber._meta, "managed", False)
    base = _base(temporary_databases)
    _execute_in(base, f"INSERT INTO {Subscriber._meta.db_table} (email) VALUES ('kept@base')")

    name = template_database(_shape(rows=40, seed=45), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert _rows_in(target, f"SELECT email FROM {Subscriber._meta.db_table}") == [("kept@base",)]


def test_a_table_postgres_stores_under_a_shortened_name_is_rebuilt(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # PostgreSQL keeps the first 63 bytes of a name, cut back to a character
    # boundary, and Django shortens only a table name it makes up, never one a
    # model spells out. Looked for under the model's own name, the base's table
    # is not there: it was left in place, and run_syncdb, which looks for it
    # the same way, failed on it with "already exists". The two-byte character
    # straddles byte 63, so a cut by characters, or one that splits it, still
    # misses.
    spelled = "testapp_subscriber_" + "x" * 43 + "\N{LATIN SMALL LETTER E WITH ACUTE}tail"
    stored = spelled.encode()[:62].decode()
    base = _base(temporary_databases)
    _execute_in(base, f'ALTER TABLE {Subscriber._meta.db_table} RENAME TO "{spelled}"')
    _execute_in(base, f"INSERT INTO \"{stored}\" (email) VALUES ('stale@base')")
    monkeypatch.setattr(Subscriber._meta, "db_table", spelled)

    name = template_database(_shape(rows=40, seed=61), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert _rows_in(base, f'SELECT count(*) FROM "{stored}"') == [(1,)]
    assert _rows_in(target, f'SELECT count(*) FROM "{stored}"') == [(0,)]


def test_an_app_with_no_models_module_is_carried(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # run_syncdb passes over an app whose models were registered from
    # somewhere other than a models module, so it never makes that app's
    # tables, and dropping them would lose them. Holds that the drop list skips
    # such an app as run_syncdb does.
    monkeypatch.setattr(apps.get_app_config("testapp"), "models_module", None)
    base = _base(temporary_databases)
    _execute_in(base, f"INSERT INTO {Subscriber._meta.db_table} (email) VALUES ('kept@base')")

    name = template_database(_shape(rows=40, seed=62), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert _rows_in(target, f"SELECT email FROM {Subscriber._meta.db_table}") == [("kept@base",)]


def _columns_of(table: str) -> str:
    """Every column of ``table`` as information_schema describes it."""
    return (
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        f"WHERE table_name = '{table}' ORDER BY column_name"
    )


def _drop_foreign_keys(database: str, table: str) -> None:
    """Drop every foreign key ``table`` holds, as a legacy table often has none.

    A table carried into the copy that points into a table being rebuilt stops
    the rebuild, which ``test_a_reference_into_such_a_table_refuses_the_base``
    holds; the tests here are about which tables are carried, so theirs go.
    """
    _execute_in(
        database,
        "DO $$ DECLARE constraint_name text; BEGIN "
        "FOR constraint_name IN SELECT conname FROM pg_constraint "
        f"WHERE conrelid = '{table}'::regclass AND contype = 'f' LOOP "
        f"EXECUTE format('ALTER TABLE {table} DROP CONSTRAINT %I', constraint_name); "
        "END LOOP; END $$",
    )


def test_a_stale_through_table_is_rebuilt_with_the_model_that_declares_it(
    temporary_databases: list[str],
) -> None:
    # run_syncdb makes an auto-created many-to-many table only while making the
    # model that declares the relation, so the copy has to drop it with that
    # model: kept, it would survive stale, and the model's own create would
    # fail on it with "already exists". Holds that those tables are dropped.
    through = Wearer.badges.through._meta.db_table
    base = _base(temporary_databases)
    _execute_in(base, f"ALTER TABLE {through} ADD COLUMN left_over integer")
    shape = _shape(rows=40, seed=49)

    from_base = template_database(shape, base=base)
    temporary_databases.append(from_base)
    from_empty = _template(shape, temporary_databases)
    targets = [f"{from_base}_end", f"{from_empty}_end"]
    temporary_databases.extend(targets)
    clone_database(from_base, targets[0])
    clone_database(from_empty, targets[1])

    assert _rows_in(targets[0], _columns_of(through)) == _rows_in(targets[1], _columns_of(through))
    assert "left_over" not in str(_rows_in(targets[0], _columns_of(through)))


def test_the_through_table_of_an_unmanaged_model_is_carried(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Django counts an auto-created through table as managed when either end
    # is, so with the declaring end unmanaged the through still reads as one
    # migrate builds -- and run_syncdb never builds it, because it builds it
    # only from the declaring model, which it skips. Dropped, it would be lost.
    # Holds that the through tables are read off the models run_syncdb makes.
    monkeypatch.setattr(Wearer._meta, "managed", False)
    through = Wearer.badges.through._meta.db_table
    base = _base(temporary_databases)
    _drop_foreign_keys(base, through)
    _execute_in(
        base,
        f"INSERT INTO {Wearer._meta.db_table} (id, name) VALUES (1, 'kept'); "
        f"INSERT INTO {through} (wearer_id, badge_id) VALUES (1, 7)",
    )
    assert Wearer.badges.through._meta.managed

    name = template_database(_shape(rows=40, seed=50), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert _rows_in(target, f"SELECT wearer_id, badge_id FROM {through}") == [(1, 7)]


def test_a_declared_through_model_is_judged_as_a_model_of_its_own(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A through model a relation names is not made by the declaring model's
    # create, so it is never dropped on that model's account: unmanaged, its
    # table is carried like any other unmanaged model's. Holds that only the
    # auto-created through tables go with the model that declares them.
    monkeypatch.setattr(Award._meta, "managed", False)
    base = _base(temporary_databases)
    _drop_foreign_keys(base, Award._meta.db_table)
    _execute_in(base, f"INSERT INTO {Award._meta.db_table} (wearer_id, badge_id) VALUES (3, 4)")

    name = template_database(_shape(rows=40, seed=51), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert _rows_in(target, f"SELECT wearer_id, badge_id FROM {Award._meta.db_table}") == [(3, 4)]


class _NotSubscribersOnDefault:
    """A database router that keeps ``Subscriber`` off the default alias."""

    def allow_migrate(
        self, db: str, app_label: str, model_name: str | None = None, **hints: object
    ) -> bool | None:
        return False if (app_label, model_name) == ("testapp", "subscriber") else None


def test_a_table_the_router_keeps_off_the_alias_is_carried(
    temporary_databases: list[str],
) -> None:
    # run_syncdb asks the router which models to make on the alias it runs on,
    # and makes none of the rest, so a table the router keeps off it is one
    # nothing would make again. Holds that the router is asked.
    base = _base(temporary_databases)
    _execute_in(base, f"INSERT INTO {Subscriber._meta.db_table} (email) VALUES ('routed@base')")

    with override_settings(DATABASE_ROUTERS=[_NotSubscribersOnDefault()]):
        name = template_database(_shape(rows=40, seed=52), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert _rows_in(target, f"SELECT email FROM {Subscriber._meta.db_table}") == [("routed@base",)]


def test_what_a_migration_did_to_a_rebuilt_table_is_lost_from_a_base_and_kept_from_empty(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The gap the docs state rather than detect. A migration of an app with
    # migrations may index a table of an app without them; the base applied it,
    # so the copy rebuilds the table and never runs the migration again. From
    # empty, run_syncdb makes the table first and the migration indexes it.
    index = Migration("0003_index_catalogue", "contenttypes")
    index.dependencies = [("contenttypes", "0002_remove_content_type_name")]
    index.operations = [
        migrations.RunSQL(
            f"CREATE INDEX catalogue_name_by_hand ON {Catalogue._meta.db_table} (name)",
            migrations.RunSQL.noop,
        )
    ]
    load_disk = MigrationLoader.load_disk

    def load_disk_with_the_index(loader: MigrationLoader) -> None:
        load_disk(loader)
        loader.disk_migrations[("contenttypes", index.name)] = index

    monkeypatch.setattr(MigrationLoader, "load_disk", load_disk_with_the_index)
    base = _base(temporary_databases)
    _migrate(base)
    indexed = (
        "SELECT count(*) FROM pg_indexes WHERE indexname = 'catalogue_name_by_hand' "
        f"AND tablename = '{Catalogue._meta.db_table}'"
    )
    assert _rows_in(base, indexed) == [(1,)]
    shape = _shape(rows=40, seed=57)

    from_base = template_database(shape, base=base)
    temporary_databases.append(from_base)
    from_empty = _template(shape, temporary_databases)
    targets = [f"{from_base}_end", f"{from_empty}_end"]
    temporary_databases.extend(targets)
    clone_database(from_base, targets[0])
    clone_database(from_empty, targets[1])

    assert _rows_in(targets[0], indexed) == [(0,)]
    assert _rows_in(targets[1], indexed) == [(1,)]


def test_a_reference_into_such_a_table_refuses_the_base(temporary_databases: list[str]) -> None:
    # Rebuilding drops the table without CASCADE. CASCADE would remove the
    # reference silently, run_syncdb would not put it back, and the template
    # would differ from both the base and one built from empty; so PostgreSQL
    # refuses the drop and the refusal is turned into one naming the base.
    base = _base(temporary_databases)
    _execute_in(
        base,
        "CREATE TABLE kept_reference "
        f"(catalogue_id bigint REFERENCES {Catalogue._meta.db_table} (id))",
    )
    before = _templates_on_the_server()

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(_shape(rows=40, seed=46), base=base))

    message = str(refused.value)
    assert base in message
    assert "kept_reference" in message
    assert "recreate it" in message
    assert "CASCADE" not in message
    assert _templates_on_the_server() == before
    assert _rows_in(base, "SELECT to_regclass('kept_reference') IS NOT NULL") == [(True,)]


def test_a_connection_already_on_the_base_does_not_stop_the_copy(
    temporary_databases: list[str],
) -> None:
    # A project's own test database is the likeliest base, so the process
    # asking for the template is the likeliest thing attached to it -- and
    # PostgreSQL refuses to copy a database anything is attached to. Holds that
    # nothing between reading the base and copying it reopens the connection.
    base = _base(temporary_databases)
    original = connection.settings_dict["NAME"]
    connection.close()
    connection.settings_dict["NAME"] = base
    try:
        connection.ensure_connection()
        name = template_database(_shape(rows=40, seed=23), base=base)
        temporary_databases.append(name)

        # And the caller's connection is left pointing where it pointed.
        assert connection.settings_dict["NAME"] == base
    finally:
        connection.close()
        connection.settings_dict["NAME"] = original

    assert _oid(name) is not None


def _squash_on_disk(monkeypatch: pytest.MonkeyPatch, *, replaced_files: bool = False) -> Migration:
    """A squash of two contenttypes migrations, added to what the loader reads from disk.

    No installed app ships one, so it is made here -- a real Migration, shaped as
    squashmigrations writes it -- and it creates a table, so whether a template
    ran it can be read back. With ``replaced_files`` the two migrations it
    replaces are on disk beside it, as they are until somebody deletes them, and
    the second creates the same table, because the squash is what they fold into.
    """
    marker = migrations.RunSQL("CREATE TABLE squash_marker (id int)", migrations.RunSQL.noop)
    squash = Migration("0003_squashed_0004", "contenttypes")
    squash.replaces = [("contenttypes", "0003_folded"), ("contenttypes", "0004_folded")]
    squash.dependencies = [("contenttypes", "0002_remove_content_type_name")]
    squash.operations = [marker]
    added = {("contenttypes", squash.name): squash}
    if replaced_files:
        first = Migration("0003_folded", "contenttypes")
        first.dependencies = [("contenttypes", "0002_remove_content_type_name")]
        second = Migration("0004_folded", "contenttypes")
        second.dependencies = [("contenttypes", "0003_folded")]
        second.operations = [marker]
        added |= {("contenttypes", first.name): first, ("contenttypes", second.name): second}
    load_disk = MigrationLoader.load_disk

    def load_disk_with_the_squash(loader: MigrationLoader) -> None:
        load_disk(loader)
        loader.disk_migrations.update(added)

    monkeypatch.setattr(MigrationLoader, "load_disk", load_disk_with_the_squash)
    return squash


_SQUASH_RAN = "SELECT to_regclass('squash_marker') IS NOT NULL"


def test_a_squash_whose_replaced_files_were_deleted_does_not_make_a_base_ahead(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The state a squash passes through on its way to being an ordinary
    # migration: the replaced files are gone, the squash still lists them in
    # ``replaces``, and every database migrated before the squash still records
    # them -- both of them, so the squash counts as applied.
    _squash_on_disk(monkeypatch)
    base = _base(temporary_databases)
    _execute_in(
        base,
        "INSERT INTO django_migrations (app, name, applied) VALUES "
        "('contenttypes', '0003_folded', now()), ('contenttypes', '0004_folded', now())",
    )

    name = template_database(_shape(rows=40, seed=26), base=base)
    temporary_databases.append(name)

    assert name.startswith(PREFIX)


def test_a_base_behind_a_squash_is_migrated_through_it(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # None of what the squash replaces is applied, so Django runs the squash
    # itself, and the replaced files being gone does not matter. Holds that a
    # squash is refused only when some of it is applied.
    _squash_on_disk(monkeypatch)
    base = _base(temporary_databases)

    name = template_database(_shape(rows=40, seed=35), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert _rows_in(target, _SQUASH_RAN) == [(True,)]


def test_a_squash_applied_in_part_is_finished_by_the_replaced_files_still_on_disk(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Django sets a partly applied squash aside and runs the rest of what it
    # replaces one migration at a time, which ends where the squash would have.
    # Holds that a partly applied squash is refused only when a migration it
    # still needs is missing from disk.
    _squash_on_disk(monkeypatch, replaced_files=True)
    base = _base(temporary_databases)
    _execute_in(
        base,
        "INSERT INTO django_migrations (app, name, applied) "
        "VALUES ('contenttypes', '0003_folded', now())",
    )

    name = template_database(_shape(rows=40, seed=36), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert _rows_in(target, _SQUASH_RAN) == [(True,)]
    assert _rows_in(
        target,
        "SELECT count(*) FROM django_migrations "
        "WHERE app = 'contenttypes' AND name = '0004_folded'",
    ) == [(1,)]


def test_a_squash_applied_in_part_with_its_replaced_files_deleted_is_refused(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Django runs a squash only when all or none of what it replaces is applied,
    # and otherwise runs the replaced migrations themselves. With their files
    # gone neither runs: the copy was built without the squash's table under a
    # key that names the squash, and from empty the same key would have it.
    _squash_on_disk(monkeypatch)
    base = _base(temporary_databases)
    _execute_in(
        base,
        "INSERT INTO django_migrations (app, name, applied) "
        "VALUES ('contenttypes', '0003_folded', now())",
    )
    before = _templates_on_the_server()

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(_shape(rows=40, seed=37), base=base))

    message = str(refused.value)
    assert base in message
    assert "contenttypes.0003_squashed_0004" in message
    assert "contenttypes.0004_folded" in message
    assert "recreate it" in message
    # The applied half is replaced by a squash on disk, so it is not ahead, and
    # the message is this check's rather than that one's.
    assert "ahead" not in message
    assert _templates_on_the_server() == before


# A squash of a squash, as squashmigrations writes one from Django 6.0: the outer
# squash lists the inner one in its replaces, beside the migration after it, and
# the loader judges both over the migrations they come down to.
_INNER = ("contenttypes", "0003_squashed_0004")
_OUTER = ("contenttypes", "0003_squashed_0005")
_NESTED_RAN = (
    "SELECT to_regclass('nested_marker_a') IS NOT NULL, to_regclass('nested_marker_b') IS NOT NULL"
)

nested_squashes = pytest.mark.skipif(
    django.VERSION < (6, 0), reason="Django resolves a squash of a squash from 6.0"
)


def _nested_squash_on_disk(
    monkeypatch: pytest.MonkeyPatch, *, on_disk: tuple[str, ...]
) -> Migration:
    """The outer squash, and whichever of the migrations under it ``on_disk`` names.

    The inner squash folds ``0003_folded`` and ``0004_folded``, which make one
    table; the outer one folds the inner one and ``0005_more``, which makes a
    second. So whether a template ran all of it, or lost the part after the
    inner squash, can be read back. ``0005_more`` depends on the inner squash,
    as a migration made while that squash was the app's latest does.
    """
    first_table = migrations.RunSQL("CREATE TABLE nested_marker_a (id int)", migrations.RunSQL.noop)
    second_table = migrations.RunSQL(
        "CREATE TABLE nested_marker_b (id int)", migrations.RunSQL.noop
    )
    after = [("contenttypes", "0002_remove_content_type_name")]
    made: dict[str, Migration] = {}
    for name, replaces, dependencies, operations in (
        ("0003_folded", [], after, []),
        ("0004_folded", [], [("contenttypes", "0003_folded")], [first_table]),
        (
            _INNER[1],
            [("contenttypes", "0003_folded"), ("contenttypes", "0004_folded")],
            after,
            [first_table],
        ),
        ("0005_more", [], [_INNER], [second_table]),
        (_OUTER[1], [_INNER, ("contenttypes", "0005_more")], after, [first_table, second_table]),
    ):
        migration = Migration(name, "contenttypes")
        migration.replaces = replaces
        migration.dependencies = dependencies
        migration.operations = operations
        made[name] = migration
    added = {("contenttypes", name): made[name] for name in (*on_disk, _OUTER[1])}
    load_disk = MigrationLoader.load_disk

    def load_disk_with_the_squashes(loader: MigrationLoader) -> None:
        load_disk(loader)
        loader.disk_migrations.update(added)

    monkeypatch.setattr(MigrationLoader, "load_disk", load_disk_with_the_squashes)
    return made[_OUTER[1]]


def test_a_squash_in_another_app_is_named_only_where_prune_declines_for_it(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The contenttypes squash lists two migrations the base applied and that
    # are gone from disk, and the ahead row is auth's. Up to Django 5.0,
    # migrate --prune declines over such a squash in any app; from 5.1 it looks
    # only at the app being pruned, so there the squash has nothing to do with
    # the ahead row and naming it would send someone to edit a migration that
    # is not the cause. Checked against migrate itself rather than against a
    # version table: the message names the squash exactly when pruning, as the
    # message says to, declines. Holds both halves of which rows the advice
    # reads, the ahead apps' from 5.1 and every app's before.
    _squash_on_disk(monkeypatch)
    base = _base(temporary_databases)
    _execute_in(
        base,
        "INSERT INTO django_migrations (app, name, applied) VALUES "
        "('contenttypes', '0003_folded', now()), ('contenttypes', '0004_folded', now()), "
        "('auth', '9999_ghost', now())",
    )

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(_shape(rows=40, seed=58), base=base))

    message = " ".join(str(refused.value).split())
    assert "auth.9999_ghost" in message
    assert "manage.py migrate auth --prune" in message
    named = "contenttypes.0003_squashed_0004" in message

    _migrate(base, "auth", "--prune")
    pruned = _rows_in(base, _GHOST_RECORDED) == [(False,)]

    assert named is not pruned
    assert named is (django.VERSION < (5, 1))


_GHOST_RECORDED = "SELECT EXISTS (SELECT FROM django_migrations WHERE name = '9999_ghost')"


def test_a_squash_listing_nothing_prune_would_remove_is_not_named_as_one_to_finish(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # --prune declines only over a squash that lists a migration it would
    # remove: one the base records as applied and that is gone from disk.
    # This squash's replaced files are still on disk -- one of them applied --
    # so pruning the ahead row works as it stands, while removing the squash's
    # replaces beside those files, as finishing it means, would leave the app
    # with two leaf nodes. Holds that a squash is named only when what it lists
    # includes a migration --prune would remove: the applied 0003_folded would
    # name it if a file on disk counted, and naming every squash of an ahead
    # app would name it regardless.
    squash = _squash_on_disk(monkeypatch, replaced_files=True)
    base = _base(temporary_databases)
    _execute_in(
        base,
        "INSERT INTO django_migrations (app, name, applied) VALUES "
        "('contenttypes', '0003_folded', now()), ('contenttypes', '9999_ghost', now())",
    )

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(_shape(rows=40, seed=59), base=base))

    message = " ".join(str(refused.value).split())
    assert "contenttypes.9999_ghost" in message
    assert "manage.py migrate contenttypes --prune" in message
    assert "still lists" not in message

    # And the remedy the message does give is the one that works.
    _migrate(base, "contenttypes", "--prune")
    name = template_database(_shape(rows=40, seed=59), base=base)
    temporary_databases.append(name)

    assert _rows_in(base, _GHOST_RECORDED) == [(False,)]
    assert _oid(name) is not None
    # While finishing the squash, as the message would have had it, splits the
    # app in two.
    squash.replaces = []
    assert "contenttypes" in MigrationLoader(None).detect_conflicts()


@nested_squashes
def test_a_base_behind_a_squash_of_a_squash_is_migrated_through_it(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing under either squash is applied, so Django runs the outer one,
    # and takes the inner one out of the graph because the outer one replaces
    # it -- not because it was set aside. Holds that a squash missing from the
    # graph is set aside only when no squash that replaces it is in use.
    _nested_squash_on_disk(monkeypatch, on_disk=(_INNER[1], "0005_more"))
    base = _base(temporary_databases)

    name = template_database(_shape(rows=40, seed=53), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert _rows_in(target, _NESTED_RAN) == [(True, True)]


@nested_squashes
def test_a_squash_of_a_squash_applied_in_part_is_finished_by_the_files_on_disk(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both squashes are set aside, and Django runs 0004 and 0005 themselves.
    # The outer squash replaces the inner one, which is missing from the graph
    # and unapplied, but it is a squash judged on its own rather than a file
    # gone from disk. Holds that a squash is never counted as a stranded
    # migration of the squash that replaces it.
    _nested_squash_on_disk(
        monkeypatch, on_disk=("0003_folded", "0004_folded", _INNER[1], "0005_more")
    )
    base = _base(temporary_databases)
    _execute_in(
        base,
        "INSERT INTO django_migrations (app, name, applied) "
        "VALUES ('contenttypes', '0003_folded', now())",
    )

    name = template_database(_shape(rows=40, seed=54), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert _rows_in(target, _NESTED_RAN) == [(True, True)]


@nested_squashes
def test_a_squash_of_a_squash_applied_in_part_with_a_replaced_file_deleted_is_refused(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The loader judges the outer squash over everything under it, so 0003
    # applied sets it aside although none of what it lists directly is: the
    # inner squash is not applied, and 0005 is not. With 0005's file gone,
    # Django finishes the inner squash from 0004 and nothing ever runs 0005, so
    # the copy would lack its table under a key that names it.
    _nested_squash_on_disk(monkeypatch, on_disk=("0003_folded", "0004_folded", _INNER[1]))
    base = _base(temporary_databases)
    _execute_in(
        base,
        "INSERT INTO django_migrations (app, name, applied) "
        "VALUES ('contenttypes', '0003_folded', now())",
    )
    before = _templates_on_the_server()

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(_shape(rows=40, seed=55), base=base))

    message = str(refused.value)
    assert "contenttypes.0003_squashed_0005" in message
    assert "contenttypes.0005_more" in message
    assert "ahead" not in message
    assert _templates_on_the_server() == before


def test_a_base_ahead_only_of_a_squash_of_a_squash_says_to_finish_the_squash(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Once only the outer squash is on disk, the rows of the migrations the
    # inner squash replaced are listed by nothing on disk, so they read as
    # ahead although Django has the base as fully migrated. --prune declines
    # while a squash still lists replaces, so the message says what to do
    # first; and following it, against the base, is what makes the base usable.
    squash = _nested_squash_on_disk(monkeypatch, on_disk=())
    base = _base(temporary_databases)
    _execute_in(
        base,
        "INSERT INTO django_migrations (app, name, applied) VALUES "
        "('contenttypes', '0003_folded', now()), ('contenttypes', '0004_folded', now()), "
        "('contenttypes', '0003_squashed_0004', now()), ('contenttypes', '0005_more', now())",
    )

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(_shape(rows=40, seed=56), base=base))

    message = " ".join(str(refused.value).split())
    assert "ahead" in message
    assert "contenttypes.0003_folded" in message
    assert (
        "A squashed migration still lists the migrations it replaces, "
        "for example contenttypes.0003_squashed_0005"
    ) in message
    assert "removing its replaces attribute" in message

    _migrate(base)
    squash.replaces = []
    _migrate(base, "contenttypes", "--prune")
    name = template_database(_shape(rows=40, seed=56), base=base)
    temporary_databases.append(name)

    assert _oid(name) is not None


def test_a_base_with_tables_but_no_django_migrations_is_refused(
    temporary_databases: list[str],
) -> None:
    # Nothing records which migrations made its tables, so migrate would create
    # them again and fail on the first with "already exists" from inside the
    # migration executor, naming a table rather than the base.
    base = _base(temporary_databases)
    _execute_in(base, "DROP TABLE django_migrations")
    before = _templates_on_the_server()

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(_shape(rows=40, seed=38), base=base))

    message = str(refused.value)
    assert base in message
    assert "django_migrations" in message
    assert "auth_group" in message
    assert _templates_on_the_server() == before


def test_a_base_whose_django_migrations_records_nothing_is_refused(
    temporary_databases: list[str],
) -> None:
    # What a base restored from pg_dump --schema-only looks like: the table is
    # there, so a check for the table alone passes, and it holds no rows, so
    # migrate would create every table again and fail on the first with
    # "already exists" from inside the executor.
    base = _base(temporary_databases)
    _execute_in(base, "DELETE FROM django_migrations")
    before = _templates_on_the_server()

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(_shape(rows=40, seed=47), base=base))

    message = str(refused.value)
    assert base in message
    assert "auth_group" in message
    assert "--schema-only" in message
    assert _templates_on_the_server() == before


def test_so_is_one_with_no_record_of_a_single_app_whose_tables_it_holds(
    temporary_databases: list[str],
) -> None:
    # The same history one app at a time, which is what a base made while an
    # app had no migrations looks like once the app gains a 0001_initial: its
    # tables are there and nothing records them. Holds that the check is made
    # per app, rather than once for the whole table.
    base = _base(temporary_databases)
    _execute_in(base, "DELETE FROM django_migrations WHERE app = 'auth'")

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(_shape(rows=40, seed=48), base=base))

    message = str(refused.value)
    assert "auth_group" in message
    # contenttypes still records its migrations, so its table is not named.
    assert "django_content_type" not in message


def test_but_an_app_whose_migrations_package_holds_no_migration_is_not(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # An app that gains an empty migrations package counts as migrated, so
    # run_syncdb stops making its tables, and has no migration for migrate to
    # run, so the tables the base made for it are left as they are and migrate
    # over the copy succeeds. Nothing would make them again, so they are
    # carried. Holds that only an app with a migration on disk is judged.
    load_disk = MigrationLoader.load_disk

    def load_disk_with_an_empty_package(loader: MigrationLoader) -> None:
        load_disk(loader)
        loader.unmigrated_apps.discard("testapp")
        loader.migrated_apps.add("testapp")

    monkeypatch.setattr(MigrationLoader, "load_disk", load_disk_with_an_empty_package)
    base = _base(temporary_databases)
    _execute_in(base, f"INSERT INTO {Subscriber._meta.db_table} (email) VALUES ('kept@base')")

    name = template_database(_shape(rows=40, seed=60), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert _rows_in(target, f"SELECT email FROM {Subscriber._meta.db_table}") == [("kept@base",)]
    assert _rows_in(target, f"SELECT count(*) FROM {Catalogue._meta.db_table}") == [(40,)]


def test_but_a_table_of_an_app_without_migrations_does_not_count(
    temporary_databases: list[str],
) -> None:
    # migrate --run-syncdb makes those tables with no django_migrations row,
    # and a project with no migrated app makes no django_migrations table at
    # all, so a base holding only them is one migrate could have produced.
    # Holds that only the tables of apps with migrations refuse a base.
    base = f"shape_base_{secrets.token_hex(4)}"
    with connection._nodb_cursor() as cursor:
        cursor.execute(f"CREATE DATABASE {connection.ops.quote_name(base)}")
    temporary_databases.append(base)
    _execute_in(base, f"CREATE TABLE {Subscriber._meta.db_table} (id int)")

    name = template_database(_shape(rows=40, seed=39), base=base)
    temporary_databases.append(name)

    assert _oid(name) is not None


# The ahead check's arithmetic, apart from any database, because the case that
# needs the exclusion -- a squash whose replaced files were deleted -- needs a
# squashed migration on disk to reach through a real base.


def test_a_migration_on_disk_is_not_ahead_of_it() -> None:
    applied = [("shop", "0001_initial"), ("shop", "0002_order")]

    assert _ahead_of_disk(applied, disk=applied, replaced=[], installed={"shop"}) == []


def test_one_recorded_and_missing_from_disk_is() -> None:
    applied = [("shop", "0003_ghost"), ("shop", "0001_initial"), ("auth", "0099_ghost")]

    assert _ahead_of_disk(
        applied, disk=[("shop", "0001_initial")], replaced=[], installed={"shop", "auth"}
    ) == [
        ("auth", "0099_ghost"),
        ("shop", "0003_ghost"),
    ]


def test_unless_a_migration_on_disk_replaces_it() -> None:
    # A squash whose replaced files were deleted leaves their records behind,
    # legitimately: the squash on disk is what they became.
    applied = [("shop", "0001_initial"), ("shop", "0002_order"), ("shop", "0001_squashed_0002")]

    assert (
        _ahead_of_disk(
            applied,
            disk=[("shop", "0001_squashed_0002")],
            replaced=[("shop", "0001_initial"), ("shop", "0002_order")],
            installed={"shop"},
        )
        == []
    )


def test_nor_one_recorded_for_an_app_that_is_not_installed() -> None:
    # Its rows outlive the app, and describe no table the checkout's models use.
    applied = [("shop", "0001_initial"), ("removedapp", "0001_initial")]

    assert (
        _ahead_of_disk(applied, disk=[("shop", "0001_initial")], replaced=[], installed={"shop"})
        == []
    )


# The stranded check's arithmetic, apart from any database, for the one condition
# a real base reaches only through a dependency Django would refuse first: a
# replaced migration applied and then deleted, under a squash set aside.


def test_one_unapplied_and_in_no_graph_is_stranded() -> None:
    squash = ("shop", "0001_squashed_0002")

    assert _stranded_squashes(
        [("shop", "0001_initial")],
        graph=[("shop", "0001_initial")],
        replacements={squash: [("shop", "0001_initial"), ("shop", "0002_order")]},
    ) == {squash: [("shop", "0002_order")]}


def test_but_one_already_applied_is_not() -> None:
    # Applied needs no running, whether or not its file is still there.
    squash = ("shop", "0001_squashed_0002")

    assert (
        _stranded_squashes(
            [("shop", "0001_initial")],
            graph=[("shop", "0002_order")],
            replacements={squash: [("shop", "0001_initial"), ("shop", "0002_order")]},
        )
        == {}
    )


def test_nor_one_that_is_itself_a_squash() -> None:
    # The inner squash of a squash of a squash is missing from the graph when it
    # is set aside, and is judged as a squash of its own rather than as a file.
    inner, outer = ("shop", "0001_squashed_0002"), ("shop", "0001_squashed_0003")

    assert (
        _stranded_squashes(
            [("shop", "0001_initial")],
            graph=[("shop", "0001_initial"), ("shop", "0002_order"), ("shop", "0003_more")],
            replacements={
                inner: [("shop", "0001_initial"), ("shop", "0002_order")],
                outer: [inner, ("shop", "0003_more")],
            },
        )
        == {}
    )


def test_nor_one_under_a_squash_the_loader_replaced_rather_than_set_aside() -> None:
    # Nothing applied: the loader uses the outer squash, which takes the inner
    # one out of the graph, and with it the migrations the inner one replaced.
    inner, outer = ("shop", "0001_squashed_0002"), ("shop", "0001_squashed_0003")

    assert (
        _stranded_squashes(
            [],
            graph=[outer],
            replacements={
                inner: [("shop", "0001_initial"), ("shop", "0002_order")],
                outer: [inner, ("shop", "0003_more")],
            },
        )
        == {}
    )
