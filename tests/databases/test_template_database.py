from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager

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
from django_data_shape.databases import template_database as template_module
from django_data_shape.databases.template_database import (
    _ahead_of_disk,
    _base_context,
    _context,
    _key,
    _schema_digest,
    _stranded_squashes,
)
from django_data_shape.utils import PARTIAL_SUFFIX, TEMPLATE_PREFIX
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
    """Read from a cloned database directly, with no Django connection involved."""
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
    """A migrated database to start templates from, dropped with everything else."""
    name = f"shape_base_{secrets.token_hex(4)}"
    source = connection.settings_dict["NAME"]
    quote = connection.ops.quote_name
    connection.close()
    with connection._nodb_cursor() as cursor:
        cursor.execute(f"CREATE DATABASE {quote(name)} TEMPLATE {quote(source)}")
    made.append(name)
    return name


def _migrate(database: str, *arguments: str) -> None:
    """Run ``migrate`` against ``database``, as a project keeping a base would."""
    original = connection.settings_dict["NAME"]
    connection.close()
    connection.settings_dict["NAME"] = database
    try:
        call_command("migrate", *arguments, interactive=False, verbosity=0)
    finally:
        connection.close()
        connection.settings_dict["NAME"] = original


def _left_by(shape: Shape, base: str | None = None) -> set[str]:
    """The databases a build of ``shape`` would leave: its template and its working copy."""
    context = template_module._context()
    if base is not None:
        context = (*context, base, str(_oid(base)))
    name = f"{TEMPLATE_PREFIX}{_key(shape, context)}"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT datname FROM pg_database WHERE datname IN (%s, %s)",
            [name, f"{name}{PARTIAL_SUFFIX}"],
        )
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

    assert name.startswith(TEMPLATE_PREFIX)
    assert _oid(name) is not None


def test_a_template_is_keyed_to_the_run_that_asked_for_it(
    temporary_databases: list[str],
) -> None:
    # The salt is in the suite-wide fixture.
    shape = _shape()
    name = _template(shape, temporary_databases)

    assert name == f"{TEMPLATE_PREFIX}{_key(shape, template_module._context())}"
    assert name != f"{TEMPLATE_PREFIX}{_key(shape, _context())}"


def test_what_a_build_of_a_shape_would_leave_is_found_wherever_it_came_from(
    temporary_databases: list[str],
) -> None:
    # A partial left by another process is found; one for another shape is not.
    shape = _shape()
    name = f"{TEMPLATE_PREFIX}{_key(shape, template_module._context())}"
    elsewhere = _shape(seed=1)
    assert _left_by(shape) == set()

    with connection._nodb_cursor() as cursor:
        for left in (name, f"{name}{PARTIAL_SUFFIX}"):
            cursor.execute(f"CREATE DATABASE {connection.ops.quote_name(left)}")
            temporary_databases.append(left)

    assert _left_by(shape) == {name, f"{name}{PARTIAL_SUFFIX}"}
    assert _left_by(elsewhere) == set()


def test_asking_twice_does_not_build_twice(temporary_databases: list[str]) -> None:
    shape = _shape()
    name = _template(shape, temporary_databases)
    first = _oid(name)

    again = template_database(shape)

    # Row counts cannot tell them apart, because a rebuild produces exactly the same rows.
    assert again == name
    assert _oid(again) == first


def test_two_shapes_do_not_share_a_template(temporary_databases: list[str]) -> None:
    assert _template(_shape(rows=40), temporary_databases) != _template(
        _shape(rows=41), temporary_databases
    )


def test_the_seed_alone_is_enough_to_make_it_a_different_database(
    temporary_databases: list[str],
) -> None:
    # Two shapes with the same cardinality and different rows in it.
    assert _template(_shape(seed=1), temporary_databases) != _template(
        _shape(seed=2), temporary_databases
    )


def test_nothing_may_connect_to_a_finished_template(temporary_databases: list[str]) -> None:
    # Turning connections off is what makes that impossible rather than unlikely.
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
    # pg_statistic is ordinary catalogue content, so it comes along.
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

    # The default destroys nothing: an existing database makes the clone fail, not vanish.
    with pytest.raises(Exception, match="already exists"):
        clone_database(name, target)

    clone_database(name, target, replace=True)

    assert _rows_in(target, f"SELECT count(*) FROM {Catalogue._meta.db_table}") == [(40,)]


def test_a_shape_whose_build_fails_leaves_no_half_built_template(
    temporary_databases: list[str],
) -> None:
    # A projection over tables nobody filled inserts nothing, which build() refuses.
    shape = Shape(Projection(EventSession, per=Event, copying=TemplateSession))

    with pytest.raises(InvalidShape, match="inserted no rows"):
        temporary_databases.append(template_database(shape))

    assert _left_by(shape) == set()


def test_a_template_holds_a_whole_graph(temporary_databases: list[str]) -> None:
    # One table proves the mechanism; the graph proves it on the thing this package is for.
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
    # A target is catalogue state like the statistics, so a clone keeps the declared one.
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
    # Nothing is created, because the name cannot be computed at all.
    shape = Shape(Table(SlugPk, rows=5, name=Constant("x"), keys=KeyFunction(lambda row: str(row))))

    with pytest.raises(UnhashableShape, match="KeyFunction"):
        temporary_databases.append(template_database(shape))


def test_it_refuses_to_run_inside_a_transaction(temporary_databases: list[str]) -> None:
    # Rather than poisoning the connection.
    with transaction.atomic(), pytest.raises(TransactionManagementError, match="atomic block"):
        temporary_databases.append(template_database(_shape()))


def test_dropping_says_whether_there_was_anything_to_drop(
    temporary_databases: list[str],
) -> None:
    name = _template(_shape(), temporary_databases)

    assert drop_database(name) is True
    # An unconditional True would make the answer, which a caller cleaning up reads, useless.
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
    # A real non-PostgreSQL alias, because a stub proves the gate, not that anything calls it.
    with pytest.raises(UnsupportedBackend):
        call()


def test_the_declaration_reaches_the_database_and_not_only_the_name(
    temporary_databases: list[str],
) -> None:
    # What a clone holds is the declared database, not one with the right name.
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
    # The refusal is about callables, not about anything a declaration ordinarily holds.
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

    assert _template(shape, temporary_databases).startswith(TEMPLATE_PREFIX)


# The key, taken apart. Each piece is a function of its arguments rather than of
# the world, which is what makes "the schema reaches the cache key" a claim a
# test can falsify instead of a sentence in a docstring.


def test_the_declaration_reaches_the_key() -> None:
    context = ("version", "schema", "True", "UTC")

    assert _key(_shape(rows=1), context) != _key(_shape(rows=2), context)


def test_and_so_does_everything_around_it() -> None:
    shape = _shape()
    context = ("version", "schema", "True", "UTC")

    assert _key(shape, context) != _key(shape, ("version", "different schema", "True", "UTC"))
    # And a base, when there is one: its name and its database oid follow the rest of the context.
    keys = {
        _key(shape, context),
        _key(shape, (*context, "base", "16384")),
        _key(shape, (*context, "base", "16385")),
        _key(shape, (*context, "other", "16384")),
    }
    assert len(keys) == 4


def test_the_schema_digest_moves_when_a_model_does() -> None:
    # Tables built from models move with them, while every migration name stays the same.
    assert _schema_digest((Catalogue,), ()) != _schema_digest((Catalogue, Event), ())


def test_and_when_a_migration_is_added() -> None:
    # The other half.
    assert _schema_digest((Catalogue,), ()) != _schema_digest(
        (Catalogue,), (("testapp", "0001_initial"),)
    )


def test_the_package_version_is_part_of_the_context() -> None:
    # A release can change the rows while the declaration stays the same word for word.
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
    # Same declaration, different database, and therefore a different key.
    before = _context()

    with override_settings(**settings_override):
        assert _context() != before


# Starting from a migrated base. A project with a long migration history pays
# for the replay on every template it builds from empty, and usually already
# keeps a migrated database for exactly that reason.


def test_a_template_can_start_from_a_migrated_base(temporary_databases: list[str]) -> None:
    base = _base(temporary_databases)
    # A row in an undeclared table tells a template built from a base from one built from empty.
    _execute_in(base, "INSERT INTO auth_group (name) VALUES ('from the base')")

    name = template_database(_shape(rows=40), base=base)
    temporary_databases.append(name)
    target = f"{name}_end"
    temporary_databases.append(target)
    clone_database(name, target)

    assert name.startswith(TEMPLATE_PREFIX)
    assert _rows_in(target, "SELECT name FROM auth_group") == [("from the base",)]
    assert _rows_in(target, f"SELECT count(*) FROM {Catalogue._meta.db_table}") == [(40,)]


def test_migrate_still_runs_over_the_copy(temporary_databases: list[str]) -> None:
    # An app with no migrations gets its tables from run_syncdb, so migrate must still run.
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
    # The same declaration from empty and from a base is two databases, so two names.
    shape = _shape(rows=40, seed=21)
    base = _base(temporary_databases)

    from_empty = _template(shape, temporary_databases)
    from_base = template_database(shape, base=base)
    temporary_databases.append(from_base)

    assert from_empty != from_base


def test_recreating_the_base_is_a_new_template(temporary_databases: list[str]) -> None:
    # A base recreated under the same name has a new oid, which the key answers.
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
    # The copy is what was migrated; the base is left where its owner put it.
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
    assert _rows_in(base, _FIRST_NAME_WIDTH) == [(30,)]


def test_migrating_the_base_forward_in_place_asks_for_the_same_template(
    temporary_databases: list[str],
) -> None:
    # Catching the base up leaves its oid alone, so the key names the same template.
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
    assert _rows_in(target, _FIRST_NAME_WIDTH) == [(150,)]


def test_an_empty_base_is_migrated_in_full(temporary_databases: list[str]) -> None:
    # A base with no django_migrations table must read as nothing applied, not fail.
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
    # The one base migrating cannot fix.
    base = _base(temporary_databases)
    _execute_in(
        base,
        "INSERT INTO django_migrations (app, name, applied) "
        "VALUES ('contenttypes', '9999_ghost', now())",
    )
    declared = _shape()

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(declared, base=base))

    message = str(refused.value)
    assert base in message
    assert "ahead" in message
    assert "contenttypes.9999_ghost" in message
    assert "manage.py migrate contenttypes --prune" in message
    assert "recreate it" in message
    assert _left_by(declared, base) == set()
    assert _left_by(declared) == set()


def test_a_base_that_goes_ahead_is_refused_with_its_template_already_built(
    temporary_databases: list[str],
) -> None:
    # The check runs before the key is looked up, so the cache hit is never reached.
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
    # A removed app's rows describe nothing this checkout uses, so they do not refuse the base.
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
    declared = _shape()

    with pytest.raises(UnusableBase, match="shape_base_that_was_never_made"):
        temporary_databases.append(
            template_database(declared, base="shape_base_that_was_never_made")
        )

    # A missing base has no oid, and a regression would fall back to building without it.
    assert _left_by(declared) == set()


def _allow_connections(database: str, allowed: bool) -> None:
    with connection._nodb_cursor() as cursor:
        cursor.execute(
            f"ALTER DATABASE {connection.ops.quote_name(database)} "
            f"WITH ALLOW_CONNECTIONS {'true' if allowed else 'false'}"
        )


def test_a_base_that_refuses_connections_is_refused_by_name(
    temporary_databases: list[str],
) -> None:
    # Nor does a refusal fall back to building the shape without its base.
    base = _base(temporary_databases)
    _allow_connections(base, False)
    declared = _shape()

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(declared, base=base))

    message = str(refused.value)
    assert base in message
    assert "ALLOW_CONNECTIONS true" in message
    assert _left_by(declared, base) == set()
    assert _left_by(declared) == set()


def test_a_template_is_never_a_base(temporary_databases: list[str]) -> None:
    # A template holds one shape's rows, so starting another from it is a mistake.
    template = _template(_shape(rows=40, seed=31), temporary_databases)

    with pytest.raises(UnusableBase, match="never a base"):
        temporary_databases.append(template_database(_shape(rows=40, seed=32), base=template))

    _allow_connections(template, True)
    with pytest.raises(UnusableBase, match="never a base"):
        temporary_databases.append(template_database(_shape(rows=40, seed=32), base=template))


def test_a_partial_this_package_left_is_never_a_base(temporary_databases: list[str]) -> None:
    # A partial left by a killed build is this package's as much as a finished template is.
    partial = f"{TEMPLATE_PREFIX}{secrets.token_hex(8)}__partial"
    with connection._nodb_cursor() as cursor:
        cursor.execute(f"CREATE DATABASE {connection.ops.quote_name(partial)}")
    temporary_databases.append(partial)

    with pytest.raises(UnusableBase, match="never a base"):
        temporary_databases.append(template_database(_shape(rows=40, seed=40), base=partial))


@pytest.mark.parametrize("pattern", ["data_shape_userdb_{}", "data_shape_{}"])
def test_a_database_only_named_like_a_template_is_not_refused_as_one(
    temporary_databases: list[str], pattern: str
) -> None:
    # Only a name of the generated shape is a template; a user's look-alike is an ordinary base.
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
    # The database exists and is migrated, so the name is the only thing wrong.
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
        with connection._nodb_cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS {escaped}")

    message = str(refused.value)
    assert "double quote" in message
    assert "does not exist" not in message


def test_a_base_holding_rows_in_a_declared_table_is_named_as_their_cause(
    temporary_databases: list[str],
) -> None:
    # Nor does a refusal fall back to building the shape without its base.
    base = _base(temporary_databases)
    _execute_in(base, "INSERT INTO auth_group (name) VALUES ('kept in the base')")
    declared = Shape(Table(Group, rows=1, name=Constant("declared")), seed=42)

    with pytest.raises(ShapeNotEmpty) as refused:
        temporary_databases.append(template_database(declared, base=base))

    message = " ".join(str(refused.value).split())
    assert message.startswith("auth_group already holds rows")
    assert (
        "a template started from a base database keeps the rows the base holds, apart from "
        "the tables it rebuilds for apps without migrations" in message
    )
    assert _left_by(declared, base) == set()
    assert _left_by(declared) == set()


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
    # Its tables are dropped from the copy and made again, as they are in an empty database.
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
    # A base contributes what its migrations contribute, and an app without any has none.
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
    # Holds that only the tables migrate builds are dropped.
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
    # PostgreSQL keeps 63 bytes of a name, so a shortened table must be looked for by that name.
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
    # Holds that the drop list skips such an app as run_syncdb does.
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
    """Drop every foreign key ``table`` holds, as a legacy table often has none."""
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
    # Holds that those tables are dropped.
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
    # Dropped, it would be lost.
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
    # Holds that only the auto-created through tables go with the model that declares them.
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
    # Holds that the router is asked.
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
    # The gap the docs state rather than detect.
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
    # Rebuilding drops the table without CASCADE.
    base = _base(temporary_databases)
    _execute_in(
        base,
        "CREATE TABLE kept_reference "
        f"(catalogue_id bigint REFERENCES {Catalogue._meta.db_table} (id))",
    )
    declared = _shape(rows=40, seed=46)

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(declared, base=base))

    message = str(refused.value)
    assert base in message
    assert "kept_reference" in message
    assert "recreate it" in message
    assert "CASCADE" not in message
    assert _left_by(declared, base) == set()
    assert _left_by(declared) == set()
    assert _rows_in(base, "SELECT to_regclass('kept_reference') IS NOT NULL") == [(True,)]


@pytest.fixture
def roles() -> Iterator[Callable[[str], str]]:
    """Make roles without superuser rights, and drop them afterwards with every database they own.

    NOLOGIN, because nothing logs in as one: the connection takes a role with
    ``SET ROLE``, through Django's own ``assume_role`` option, so a role needs
    no password and no authentication rule on whichever server the suite runs
    against -- only a suite role that may create roles, which CI's ``postgres``
    user and a local superuser both are. The argument adds attributes:
    ``CREATEDB`` for a role that makes templates.

    Every database one of these roles owns when the test ends was made by the
    test or handed to the role by it, since the role did not exist before the
    test; they go first, because PostgreSQL refuses to drop a role that still
    owns one, or owns anything inside one. The connection's role is put back
    before anything is dropped, in case the test stopped inside
    :func:`_acting_as`.
    """
    made: list[str] = []

    def make(attributes: str = "") -> str:
        role = f"shape_role_{secrets.token_hex(4)}"
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE ROLE {role} NOLOGIN {attributes}")
        made.append(role)
        return role

    yield make
    connection.settings_dict["OPTIONS"].pop("assume_role", None)
    connection.close()
    with connection._nodb_cursor() as cursor:
        cursor.execute("SELECT datname FROM pg_database WHERE datdba = ANY (%s::regrole[])", [made])
        for (owned,) in cursor.fetchall():
            cursor.execute(f"DROP DATABASE IF EXISTS {connection.ops.quote_name(owned)}")
        for role in reversed(made):
            cursor.execute(f"DROP ROLE IF EXISTS {role}")


@contextmanager
def _acting_as(role: str) -> Iterator[None]:
    """Every connection the default alias opens inside the block is ``role``, by ``SET ROLE``."""
    options = connection.settings_dict["OPTIONS"]
    connection.close()
    options["assume_role"] = role
    try:
        yield
    finally:
        options.pop("assume_role", None)
        connection.close()


def _owned_by(role: str) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT datname FROM pg_database WHERE datdba = %s::regrole ORDER BY datname", [role]
        )
        return [row[0] for row in cursor.fetchall()]


def _base_for(role: str, made: list[str], *, schema_owner: str, rebuilt_owner: str) -> str:
    """A base ``role`` may copy and migrate, whose rebuilt tables ``rebuilt_owner`` owns."""
    base = _base(made)
    quote = connection.ops.quote_name
    with connection._nodb_cursor() as cursor:
        cursor.execute(f"ALTER DATABASE {quote(base)} OWNER TO {role}")
    tables = _rows_in(base, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    _execute_in(
        base,
        "; ".join(
            [
                f"ALTER SCHEMA public OWNER TO {schema_owner}",
                f"GRANT USAGE, CREATE ON SCHEMA public TO {role}",
                *(
                    f"ALTER TABLE {quote(str(name))} OWNER TO "
                    f"{rebuilt_owner if str(name).startswith('testapp') else role}"
                    for (name,) in tables
                ),
            ]
        ),
    )
    return base


def _rebuilt_tables_of(base: str) -> list[tuple[object, ...]]:
    """The base's tables of the app without migrations, with their owners, in byte order."""
    return _rows_in(
        base,
        "SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'public' "
        "AND tablename LIKE 'testapp%' ORDER BY tablename COLLATE \"C\"",
    )


def test_a_base_whose_rebuilt_tables_the_role_cannot_drop_is_refused_naming_their_owner(
    roles: Callable[[str], str], temporary_databases: list[str]
) -> None:
    # TEMPLATE keeps every owner the base gave.
    role = roles("CREATEDB")
    base = _base_for(
        role, temporary_databases, schema_owner="CURRENT_USER", rebuilt_owner="CURRENT_USER"
    )
    rebuilt = _rebuilt_tables_of(base)

    with _acting_as(role), pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(_shape(rows=40, seed=61), base=base))

    message = str(refused.value)
    first, owner = rebuilt[0]
    assert base in message
    assert f"{len(rebuilt)} table(s)" in message
    assert f"{first} (owned by {owner})" in message
    assert repr(role) in message
    assert "OWNER TO" in message
    assert "base=None" in message
    assert _owned_by(role) == [base]


def test_so_is_one_that_is_a_member_of_their_owner_without_inheriting_its_privileges(
    roles: Callable[[str], str], temporary_databases: list[str]
) -> None:
    # Ownership is checked as having the owner's privileges, not as mere membership.
    owner = roles()
    role = roles("CREATEDB NOINHERIT")
    with connection.cursor() as cursor:
        cursor.execute(f"GRANT {owner} TO {role}")
    base = _base_for(role, temporary_databases, schema_owner="CURRENT_USER", rebuilt_owner=owner)

    with _acting_as(role), pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(_shape(rows=40, seed=64), base=base))

    assert f"(owned by {owner})" in str(refused.value)
    assert _owned_by(role) == [base]


def test_a_role_owning_those_tables_may_rebuild_them_without_owning_their_schema(
    roles: Callable[[str], str], temporary_databases: list[str]
) -> None:
    # The table-owner half of the rule.
    role = roles("CREATEDB")
    base = _base_for(role, temporary_databases, schema_owner="CURRENT_USER", rebuilt_owner=role)

    with _acting_as(role):
        name = template_database(_shape(rows=40, seed=62), base=base)
    temporary_databases.append(name)

    assert _owned_by(role) == sorted([base, name])


def test_so_may_one_owning_their_schema_without_owning_them(
    roles: Callable[[str], str], temporary_databases: list[str]
) -> None:
    # From PostgreSQL 15 the public schema belongs to pg_database_owner, the common case here.
    role = roles("CREATEDB")
    base = _base_for(
        role, temporary_databases, schema_owner="pg_database_owner", rebuilt_owner="CURRENT_USER"
    )

    with _acting_as(role):
        name = template_database(_shape(rows=40, seed=63), base=base)
    temporary_databases.append(name)

    assert _owned_by(role) == sorted([base, name])


def test_a_connection_already_on_the_base_does_not_stop_the_copy(
    temporary_databases: list[str],
) -> None:
    # Holds that nothing between reading the base and copying it reopens the connection.
    base = _base(temporary_databases)
    original = connection.settings_dict["NAME"]
    connection.close()
    connection.settings_dict["NAME"] = base
    try:
        connection.ensure_connection()
        name = template_database(_shape(rows=40, seed=23), base=base)
        temporary_databases.append(name)

        assert connection.settings_dict["NAME"] == base
    finally:
        connection.close()
        connection.settings_dict["NAME"] = original

    assert _oid(name) is not None


def _squash_on_disk(monkeypatch: pytest.MonkeyPatch, *, replaced_files: bool = False) -> Migration:
    """A squash of two contenttypes migrations, added to what the loader reads from disk."""
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
    # Replaced files gone but still listed, so the squash counts as applied and nothing is ahead.
    _squash_on_disk(monkeypatch)
    base = _base(temporary_databases)
    _execute_in(
        base,
        "INSERT INTO django_migrations (app, name, applied) VALUES "
        "('contenttypes', '0003_folded', now()), ('contenttypes', '0004_folded', now())",
    )

    name = template_database(_shape(rows=40, seed=26), base=base)
    temporary_databases.append(name)

    assert name.startswith(TEMPLATE_PREFIX)


def test_a_base_behind_a_squash_is_migrated_through_it(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Holds that a squash is refused only when some of it is applied.
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
    # A partly applied squash is set aside, and the replaced files still on disk finish it.
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
    # Nor does a refusal fall back to building the shape without its base.
    _squash_on_disk(monkeypatch)
    base = _base(temporary_databases)
    _execute_in(
        base,
        "INSERT INTO django_migrations (app, name, applied) "
        "VALUES ('contenttypes', '0003_folded', now())",
    )
    declared = _shape(rows=40, seed=37)

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(declared, base=base))

    message = str(refused.value)
    assert base in message
    assert "contenttypes.0003_squashed_0004" in message
    assert "contenttypes.0004_folded" in message
    assert "recreate it" in message
    assert "ahead" not in message
    assert _left_by(declared, base) == set()
    assert _left_by(declared) == set()


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
    """The outer squash, and whichever of the migrations under it ``on_disk`` names."""
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
    # Up to Django 5.0, --prune declines over a squash in any app, so only there is it named.
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
    # And the remedy the message does give is the one that works.
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

    _migrate(base, "contenttypes", "--prune")
    name = template_database(_shape(rows=40, seed=59), base=base)
    temporary_databases.append(name)

    assert _rows_in(base, _GHOST_RECORDED) == [(False,)]
    assert _oid(name) is not None
    squash.replaces = []
    assert "contenttypes" in MigrationLoader(None).detect_conflicts()


@nested_squashes
def test_a_base_behind_a_squash_of_a_squash_is_migrated_through_it(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Django runs the outer squash, which replaces the inner, so the base migrates through it.
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
    # Nor does a refusal fall back to building the shape without its base.
    _nested_squash_on_disk(monkeypatch, on_disk=("0003_folded", "0004_folded", _INNER[1]))
    base = _base(temporary_databases)
    _execute_in(
        base,
        "INSERT INTO django_migrations (app, name, applied) "
        "VALUES ('contenttypes', '0003_folded', now())",
    )
    declared = _shape(rows=40, seed=55)

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(declared, base=base))

    message = str(refused.value)
    assert "contenttypes.0003_squashed_0005" in message
    assert "contenttypes.0005_more" in message
    assert "ahead" not in message
    assert _left_by(declared, base) == set()
    assert _left_by(declared) == set()


def test_a_base_ahead_only_of_a_squash_of_a_squash_says_to_finish_the_squash(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the outer squash on disk leaves the inner's rows reading as ahead; say to finish it.
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
    # Nor does a refusal fall back to building the shape without its base.
    base = _base(temporary_databases)
    _execute_in(base, "DROP TABLE django_migrations")
    declared = _shape(rows=40, seed=38)

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(declared, base=base))

    message = str(refused.value)
    assert base in message
    assert "django_migrations" in message
    assert "auth_group" in message
    assert _left_by(declared, base) == set()
    assert _left_by(declared) == set()


def test_a_base_whose_django_migrations_records_nothing_is_refused(
    temporary_databases: list[str],
) -> None:
    # Nor does a refusal fall back to building the shape without its base.
    base = _base(temporary_databases)
    _execute_in(base, "DELETE FROM django_migrations")
    declared = _shape(rows=40, seed=47)

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(declared, base=base))

    message = str(refused.value)
    assert base in message
    assert "auth_group" in message
    assert "--schema-only" in message
    assert _left_by(declared, base) == set()
    assert _left_by(declared) == set()


def test_so_is_one_with_no_record_of_a_single_app_whose_tables_it_holds(
    temporary_databases: list[str],
) -> None:
    # Holds that the check is made per app, rather than once for the whole table.
    base = _base(temporary_databases)
    _execute_in(base, "DELETE FROM django_migrations WHERE app = 'auth'")

    with pytest.raises(UnusableBase) as refused:
        temporary_databases.append(template_database(_shape(rows=40, seed=48), base=base))

    message = str(refused.value)
    assert "auth_group" in message
    assert "django_content_type" not in message


def test_but_an_app_whose_migrations_package_holds_no_migration_is_not(
    temporary_databases: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing would make them again, so they are carried.
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
    # A squash whose replaced files were deleted leaves their records behind, legitimately.
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
    # A squash set aside from the graph is judged as a squash, not as a file.
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
    # Nothing applied, so the loader uses the outer squash, which replaces the inner one.
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
