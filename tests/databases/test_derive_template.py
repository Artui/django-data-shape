from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator

import psycopg
import pytest
from django.db import connection, connections, transaction
from django.db.transaction import TransactionManagementError

from django_data_shape import (
    Constant,
    Shape,
    StepFailed,
    Table,
    UnsupportedBackend,
    UnusableBase,
    clone_database,
    derive_template,
    drop_database,
    template_database,
)
from django_data_shape.databases.derive_template import _key as _derived_key
from django_data_shape.utils import FINISHED_TEMPLATE, PARTIAL_SUFFIX, TEMPLATE_PREFIX
from tests.testapp.models import Catalogue, Company

# transaction=True for the reason test_template_database gives: deriving points
# the connection at another database and closes it, which an atomic block
# cannot survive. The refusal has a test of its own below.
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="CREATE DATABASE ... TEMPLATE has no equivalent on another backend",
    ),
]

CATALOGUE = Catalogue._meta.db_table
COMPANY = Company._meta.db_table


def _shape(seed: int = 41) -> Shape:
    # A column the application owns, declared as a placeholder until the step has run.
    return Shape(Table(Catalogue, rows=400, name=Constant("placeholder")), seed=seed)


def _replay(using: str) -> None:
    """What an application's own code does to a finished world, in two statements."""
    with connections[using].cursor() as cursor:
        cursor.execute(
            f"UPDATE {CATALOGUE} SET name = CASE WHEN id % 4 = 0 THEN 'cog' ELSE 'widget' END"
        )
        cursor.execute(f"INSERT INTO {COMPANY} (name) SELECT 'acme' FROM generate_series(1, 300)")


@pytest.fixture
def made() -> Iterator[list[str]]:
    """Every database a test makes, dropped afterwards, last made first."""
    names: list[str] = []
    yield names
    for name in reversed(names):
        drop_database(name)


def _template(made: list[str], seed: int = 41) -> str:
    name = template_database(_shape(seed))
    made.append(name)
    return name


def _derive(made: list[str], template: str, step: Callable[[str], object], key: str) -> str:
    name = derive_template(template, step, key=key)
    made.append(name)
    return name


def _read(made: list[str], template: str, statement: str) -> list[tuple[object, ...]]:
    """What a database cloned from ``template`` holds, read with no Django connection."""
    target = f"derived_read_{secrets.token_hex(4)}"
    clone_database(template, target)
    made.append(target)
    settings = connection.settings_dict
    with (
        psycopg.connect(
            dbname=target,
            host=settings["HOST"] or None,
            port=settings["PORT"] or None,
            user=settings["USER"] or None,
            password=settings["PASSWORD"] or None,
        ) as opened,
        opened.cursor() as cursor,
    ):
        cursor.execute(statement)
        return cursor.fetchall()


def _names_in(made: list[str], template: str) -> list[tuple[object, ...]]:
    return _read(made, template, f"SELECT name, count(*) FROM {CATALOGUE} GROUP BY name ORDER BY 1")


def _database(name: str) -> tuple[int, bool] | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT oid, datallowconn FROM pg_database WHERE datname = %s", [name])
        row = cursor.fetchone()
    return None if row is None else (int(row[0]), bool(row[1]))


def _partials(parent: str, key: str = "replay-1") -> list[str]:
    """The working copy a derivation of ``parent`` under ``key`` would leave, if one is there."""
    partial = f"{TEMPLATE_PREFIX}{_derived_key(parent, key)}{PARTIAL_SUFFIX}"
    with connection.cursor() as cursor:
        cursor.execute("SELECT datname FROM pg_database WHERE datname = %s", [partial])
        return [row[0] for row in cursor.fetchall()]


def test_the_step_runs_over_a_copy_and_its_writes_are_the_template(made: list[str]) -> None:
    parent = _template(made)

    derived = _derive(made, parent, _replay, key="replay-1")

    assert _names_in(made, derived) == [("cog", 100), ("widget", 300)]
    assert _read(made, derived, f"SELECT count(*) FROM {COMPANY}") == [(300,)]
    # The parent is a copy's source, never its subject: the build stays there to derive from.
    assert _names_in(made, parent) == [("placeholder", 400)]


def test_the_statistics_describe_the_world_after_the_step(made: list[str]) -> None:
    derived = _derive(made, _template(made), _replay, key="replay-1")

    # And the table the shape never named.
    values = _read(
        made,
        derived,
        "SELECT most_common_vals::text FROM pg_stats "
        f"WHERE tablename = '{CATALOGUE}' AND attname = 'name'",
    )
    assert values != []
    assert "widget" in str(values[0][0])
    assert "placeholder" not in str(values[0][0])
    assert _read(made, derived, f"SELECT count(*) FROM pg_stats WHERE tablename = '{COMPANY}'") != [
        (0,)
    ]


def test_asking_twice_runs_the_step_once(made: list[str]) -> None:
    parent = _template(made)
    runs: list[str] = []

    def step(using: str) -> None:
        runs.append(using)
        _replay(using)

    first = _derive(made, parent, step, key="replay-1")
    oid = _database(first)

    again = derive_template(parent, step, key="replay-1")

    # Only the database's oid tells a hit from a miss: a rebuild keeps the same name.
    assert again == first
    assert _database(again) == oid
    assert runs == ["default"]


def test_the_name_is_a_template_name_keyed_on_the_parent_and_the_key(made: list[str]) -> None:
    parent = _template(made)
    other_parent = _template(made, seed=42)

    first = _derive(made, parent, _replay, key="replay-1")
    other_key = _derive(made, parent, _replay, key="replay-2")
    other_world = _derive(made, other_parent, _replay, key="replay-1")

    assert FINISHED_TEMPLATE.fullmatch(first)
    assert len({parent, other_parent, first, other_key, other_world}) == 5


def test_a_derived_template_is_never_a_base(made: list[str]) -> None:
    derived = _derive(made, _template(made), _replay, key="replay-1")

    with pytest.raises(UnusableBase, match="is a template this package made"):
        template_database(_shape(seed=43), base=derived)


def test_steps_chain_because_a_derived_template_is_a_template(made: list[str]) -> None:
    replayed = _derive(made, _template(made), _replay, key="replay-1")

    def rename(using: str) -> None:
        with connections[using].cursor() as cursor:
            cursor.execute(f"UPDATE {CATALOGUE} SET name = 'gear' WHERE name = 'cog'")

    renamed = _derive(made, replayed, rename, key="rename-1")

    assert _names_in(made, renamed) == [("gear", 100), ("widget", 300)]
    assert _names_in(made, replayed) == [("cog", 100), ("widget", 300)]


def test_nothing_may_connect_to_a_derived_template(made: list[str]) -> None:
    derived = _derive(made, _template(made), _replay, key="replay-1")

    assert _database(derived) is not None
    assert _database(derived)[1] is False


def test_the_step_is_handed_the_alias_pointed_at_the_copy(made: list[str]) -> None:
    parent = _template(made)
    original = connection.settings_dict["NAME"]
    seen: list[tuple[str, str, bool]] = []

    def step(using: str) -> None:
        name = connections[using].settings_dict["NAME"]
        seen.append((using, name, _database(name)[1]))

    derived = _derive(made, parent, step, key="look-1")

    # The copy is open to connections while the step runs, because the step has to connect.
    assert seen == [("default", f"{derived}{PARTIAL_SUFFIX}", True)]
    assert connection.settings_dict["NAME"] == original


def test_a_step_that_raises_leaves_nothing_and_names_the_key(made: list[str]) -> None:
    parent = _template(made)
    original = connection.settings_dict["NAME"]

    def broken(using: str) -> None:
        _replay(using)
        raise LookupError("no such project")

    with pytest.raises(StepFailed, match="'replay-1'") as raised:
        made.append(derive_template(parent, broken, key="replay-1"))

    assert isinstance(raised.value.__cause__, LookupError)
    assert _partials(parent) == []
    assert connection.settings_dict["NAME"] == original
    assert _names_in(made, parent) == [("placeholder", 400)]

    # Nothing was cached, so the same key runs the step again.
    runs: list[str] = []

    def fixed(using: str) -> None:
        runs.append(using)
        _replay(using)

    derived = _derive(made, parent, fixed, key="replay-1")

    assert runs == ["default"]
    assert _names_in(made, derived) == [("cog", 100), ("widget", 300)]


class _Interrupted(BaseException):
    """Stands in for a KeyboardInterrupt, which pytest would take as its own."""


def test_an_interrupt_is_not_wrapped_and_leaves_nothing(made: list[str]) -> None:
    parent = _template(made)

    def interrupted(using: str) -> None:
        raise _Interrupted

    with pytest.raises(_Interrupted):
        made.append(derive_template(parent, interrupted, key="replay-1"))

    assert _partials(parent) == []


def test_a_hit_needs_no_parent(made: list[str]) -> None:
    parent = _template(made)
    derived = _derive(made, parent, _replay, key="replay-1")
    drop_database(parent)

    def never(using: str) -> None:
        raise AssertionError("a hit runs no step")

    assert derive_template(parent, never, key="replay-1") == derived


def test_a_partial_left_by_a_killed_process_is_replaced(made: list[str]) -> None:
    parent = _template(made)
    derived = _derive(made, parent, _replay, key="replay-1")
    drop_database(derived)
    partial = f"{derived}{PARTIAL_SUFFIX}"
    made.append(partial)
    with connection._nodb_cursor() as cursor:
        cursor.execute(f"CREATE DATABASE {connection.ops.quote_name(partial)}")

    assert _derive(made, parent, _replay, key="replay-1") == derived
    assert _partials(parent) == []


def test_it_can_copy_with_the_servers_own_strategy(made: list[str]) -> None:
    derived = derive_template(_template(made), _replay, key="replay-1", strategy=None)
    made.append(derived)

    assert _names_in(made, derived) == [("cog", 100), ("widget", 300)]


@pytest.mark.parametrize(
    "parent",
    [
        "postgres",
        f"{TEMPLATE_PREFIX}userdb",
        f"{TEMPLATE_PREFIX}{'0' * 16}{PARTIAL_SUFFIX}",
        f'"{TEMPLATE_PREFIX}{"0" * 16}"',
    ],
)
def test_a_parent_this_package_did_not_finish_is_refused(parent: str) -> None:
    # A database of the caller's own keeps its name while its rows change.
    def never(using: str) -> None:
        raise AssertionError("a refused parent runs no step")

    with pytest.raises(ValueError, match="not a template this package finished"):
        derive_template(parent, never, key="replay-1")


def test_a_parent_that_does_not_exist_is_refused_by_name(made: list[str]) -> None:
    parent = _template(made)
    drop_database(parent)

    def never(using: str) -> None:
        raise AssertionError("a missing parent runs no step")

    with pytest.raises(ValueError, match="does not exist"):
        made.append(derive_template(parent, never, key="replay-1"))
    assert _partials(parent) == []


def test_a_strategy_it_cannot_take_is_refused_before_anything_is_looked_up(
    made: list[str],
) -> None:
    parent = _template(made)
    _derive(made, parent, _replay, key="replay-1")

    # A hit would serve the derived template, so only a check made before the lookup refuses.
    with pytest.raises(ValueError, match="clone strategy"):
        derive_template(parent, _replay, key="replay-1", strategy="fast")


def test_it_refuses_to_run_inside_a_transaction(made: list[str]) -> None:
    parent = _template(made)

    with transaction.atomic(), pytest.raises(TransactionManagementError):
        derive_template(parent, _replay, key="replay-1")


@pytest.mark.django_db(transaction=True, databases=["default", "not_postgres"])
def test_it_is_not_offered_on_a_backend_without_template_databases() -> None:
    with pytest.raises(UnsupportedBackend):
        derive_template(f"{TEMPLATE_PREFIX}{'0' * 16}", _replay, key="k", using="not_postgres")
