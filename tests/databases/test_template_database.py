"""Build once per machine, clone per session, and never serve a stale database."""

from __future__ import annotations

import secrets
from collections.abc import Iterator

import psycopg
import pytest
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
)
from django_data_shape.version import __version__
from tests.testapp.models import (
    Catalogue,
    Company,
    Event,
    EventSession,
    SlugPk,
    Subscriber,
    Template,
    TemplateSession,
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
        "VALUES ('removedapp', '0001_initial', now())",
    )

    name = template_database(_shape(rows=40, seed=28), base=base)
    temporary_databases.append(name)

    assert _oid(name) is not None


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
        "a template started from a base database keeps the rows the base holds in tables "
        "of apps with migrations" in message
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


def _squash_on_disk(monkeypatch: pytest.MonkeyPatch, *, replaced_files: bool = False) -> None:
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
