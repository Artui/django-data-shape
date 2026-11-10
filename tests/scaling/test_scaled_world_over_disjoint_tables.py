from __future__ import annotations

import contextlib
from collections.abc import Callable
from functools import partial
from typing import cast

import pytest
from django.db.models import Model

import django_data_shape
from django_data_shape import (
    Constant,
    FanOut,
    KeyStrategy,
    Shape,
    Table,
    UuidKeys,
    Zipf,
    build,
    scaled_world,
)
from tests.testapp.models import (
    Company,
    Depot,
    Event,
    Region,
    Remark,
    SessionNote,
    Template,
    Tenant,
    TenantRecord,
    Thread,
    UuidSession,
)

pytestmark = pytest.mark.django_db(databases=["default", "not_postgres"])

_ALIASES = ["default", "not_postgres"]


def _graph(label: str, *, notes: bool = False) -> Shape:
    """Template, event and a UUID-keyed session, as one application declares them."""
    tables = [
        Table(Template, rows=1, name=Constant(label)),
        Table(Event, rows=2, template=FanOut(Zipf()), name=Constant(label)),
        Table(UuidSession, rows=3, event=FanOut(Zipf()), title=Constant(label)),
    ]
    if notes:
        tables.append(Table(SessionNote, rows=4, session=FanOut(Zipf()), text=Constant(label)))
    return Shape(*tables, seed=5)


def _rows(alias: str) -> list[list[tuple[object, ...]]]:
    return [
        sorted(Template.objects.using(alias).values_list("pk", "name")),
        sorted(Event.objects.using(alias).values_list("pk", "template_id", "name")),
        sorted(UuidSession.objects.using(alias).values_list("pk", "event_id", "title"), key=str),
        sorted(SessionNote.objects.using(alias).values_list("pk", "session_id", "text"), key=str),
    ]


def _labels(alias: str) -> list[list[str]]:
    return [
        sorted(Template.objects.using(alias).values_list("name", flat=True)),
        sorted(Event.objects.using(alias).values_list("name", flat=True)),
        sorted(UuidSession.objects.using(alias).values_list("title", flat=True)),
        sorted(SessionNote.objects.using(alias).values_list("text", flat=True)),
    ]


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_session_world_with_a_disjoint_child_sits_under_the_same_graph(alias: str) -> None:
    # Sessions were left alone for their keys, yet their references refused the world.
    build(_graph("session"), using=alias, require_statistics=False)
    session_world = _rows(alias)

    try:
        with scaled_world(_graph("world"), 1, using=alias) as rows:
            assert rows == 6
            assert _labels(alias) == [["world"], ["world"] * 2, ["world"] * 3, []]
    except Exception as error:
        pytest.fail(f"{type(error).__name__}: {error}")

    assert _rows(alias) == session_world


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_disjoint_table_reached_through_another_joins_too(alias: str) -> None:
    # The notes point at sessions, not at events, so they are in the set only once the sessions are.
    build(_graph("session", notes=True), using=alias, require_statistics=False)
    session_world = _rows(alias)

    try:
        with scaled_world(_graph("world", notes=True), 1, using=alias) as rows:
            assert rows == 10
            assert _labels(alias) == [["world"], ["world"] * 2, ["world"] * 3, ["world"] * 4]
    except Exception as error:
        pytest.fail(f"{type(error).__name__}: {error}")

    assert _rows(alias) == session_world


@pytest.mark.parametrize("alias", _ALIASES)
def test_an_empty_disjoint_table_carries_nothing_into_the_set(alias: str) -> None:
    # It keeps its row, beside the world's notes.
    template = Template.objects.using(alias).create(name="caller")
    Event.objects.using(alias).create(template=template, name="caller")
    SessionNote.objects.using(alias).create(session=None, text="caller")
    callers = _rows(alias)

    with scaled_world(_graph("world", notes=True), 1, using=alias):
        assert _labels(alias) == [
            ["world"],
            ["world"] * 2,
            ["world"] * 3,
            ["caller"] + ["world"] * 4,
        ]

    assert _rows(alias) == callers


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_disjoint_parent_the_world_does_not_reach_keeps_its_rows(alias: str) -> None:
    # The world's records only.
    tenant = Tenant.objects.using(alias).create(name="caller")
    TenantRecord.objects.using(alias).create(tenant=tenant, label="caller")
    shape = Shape(
        Table(Tenant, rows=2, name=Constant("world")),
        Table(TenantRecord, rows=4, tenant=FanOut(Zipf()), label=Constant("world")),
        seed=5,
    )

    with scaled_world(shape, 1, using=alias):
        assert sorted(Tenant.objects.using(alias).values_list("name", flat=True)) == [
            "caller",
            "world",
            "world",
        ]
        assert (
            list(TenantRecord.objects.using(alias).values_list("label", flat=True)) == ["world"] * 4
        )

    assert list(Tenant.objects.using(alias).values_list("pk", "name")) == [(tenant.pk, "caller")]
    assert list(TenantRecord.objects.using(alias).values_list("tenant_id", "label")) == [
        (tenant.pk, "caller")
    ]


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_key_the_database_does_not_enforce_still_joins_a_disjoint_table(alias: str) -> None:
    # The join reads the models' keys, so the threads are emptied with the companies.
    company = Company.objects.using(alias).create(name="caller")
    thread = Thread.objects.using(alias).create(company=company, title="caller")
    Remark.objects.using(alias).create(thread=thread, text="caller")
    shape = Shape(
        Table(Company, rows=2, name=Constant("world")),
        Table(Thread, rows=2, company=FanOut(Zipf()), title=Constant("world")),
        seed=5,
    )

    with pytest.raises(Exception) as refused, contextlib.ExitStack() as entering:
        entering.enter_context(scaled_world(shape, 1, using=alias))

    assert refused.type is django_data_shape.ShapeReferenced
    assert "(testapp_remark.thread_id -> testapp_thread)" in str(refused.value)


def _tenants(label: str, tenants: int, *, seed: int = 5) -> Shape:
    """UUID-keyed tenants, and records with integer keys pointing at them."""
    return Shape(
        Table(Tenant, rows=tenants, name=Constant(label)),
        Table(TenantRecord, rows=tenants * 4, tenant=FanOut(Zipf()), label=Constant(label)),
        seed=seed,
    )


def _regions(label: str, regions: int) -> Shape:
    """A graph keyed by UUIDs throughout, so every table in it is Disjoint."""
    return Shape(
        Table(Region, rows=regions, name=Constant(label)),
        Table(Depot, rows=regions * 2, region=FanOut(Zipf()), name=Constant(label)),
        seed=5,
    )


# What each model's rows are labelled by, then the key each holds into its parent.
_COLUMNS: dict[type[Model], tuple[str, ...]] = {
    Tenant: ("name",),
    TenantRecord: ("label", "tenant_id"),
    Region: ("name",),
    Depot: ("name", "region_id"),
}


def _pairs(model: type[Model], alias: str, label: str) -> list[tuple[object, ...]]:
    """Every row of ``model`` carrying ``label``, as ``(pk, label[, parent])``, sorted."""
    columns = _COLUMNS[model]
    rows = model.objects.using(alias).filter(**{columns[0]: label}).values_list("pk", *columns)
    return sorted(rows, key=str)


def _pks(model: type[Model], alias: str, **where: object) -> set[object]:
    return set(model.objects.using(alias).filter(**where).values_list("pk", flat=True))


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_session_world_with_a_disjoint_root_sits_under_the_same_graph(alias: str) -> None:
    build(_tenants("session", 50), using=alias, require_statistics=False)
    tenants, records = _pairs(Tenant, alias, "session"), _pairs(TenantRecord, alias, "session")

    with scaled_world(_tenants("world", 2), 1, using=alias) as rows:
        assert rows == 10
        assert _pairs(Tenant, alias, "session") == tenants
        assert len(_pks(Tenant, alias, name="world")) == 2
        # The session's tenants, untouched, and the world's two beside them.
        assert _pairs(TenantRecord, alias, "session") == []
        assert {tenant for _, _, tenant in _pairs(TenantRecord, alias, "world")} <= _pks(
            Tenant, alias
        )
        assert TenantRecord.objects.using(alias).count() == 8

    assert _pairs(Tenant, alias, "session") == tenants
    assert _pairs(TenantRecord, alias, "session") == records
    assert _pks(Tenant, alias, name="world") == set()


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_session_world_keyed_by_uuids_throughout_sits_under_the_same_graph(alias: str) -> None:
    # Every table is Disjoint and none points into anything emptied, so nothing is emptied.
    build(_regions("session", 50), using=alias, require_statistics=False)
    regions, depots = _pairs(Region, alias, "session"), _pairs(Depot, alias, "session")

    with scaled_world(_regions("world", 2), 1, using=alias) as rows:
        assert rows == 6
        assert _pairs(Region, alias, "session") == regions
        assert _pairs(Depot, alias, "session") == depots
        assert len(_pks(Region, alias, name="world")) == 2
        assert {region for _, _, region in _pairs(Depot, alias, "world")} <= _pks(Region, alias)
        assert len(_pks(Depot, alias, name="world")) == 4

    assert _pairs(Region, alias, "session") == regions
    assert _pairs(Depot, alias, "session") == depots
    assert Region.objects.using(alias).count() == 50


@pytest.mark.parametrize("alias", _ALIASES)
def test_with_another_seed_a_disjoint_root_builds_beside_the_session_rows(alias: str) -> None:
    # Another seed builds beside the session's tenants, so the fan-out reads both.
    build(_tenants("session", 50, seed=6), using=alias, require_statistics=False)

    with scaled_world(_tenants("world", 2), 1, using=alias) as rows:
        assert rows == 10
        assert Tenant.objects.using(alias).count() == 52
        assert TenantRecord.objects.using(alias).filter(label="session").count() == 0
        assert TenantRecord.objects.using(alias).filter(tenant__name="session").exists()


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_worlds_children_point_at_its_own_disjoint_keys(alias: str) -> None:
    # A child reads its parents' keys from the table, so no child needs changing.
    with scaled_world(_regions("world", 2), 1, using=alias):
        regions = _pks(Region, alias)
        pointed_at = {region for _, _, region in _pairs(Depot, alias, "world")}
    build(_regions("built", 2), using=alias, require_statistics=False)

    assert len(regions) == 2
    assert pointed_at and pointed_at <= regions
    assert not regions & _pks(Region, alias)


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_worlds_disjoint_keys_are_the_same_at_every_factor(alias: str) -> None:
    # Over the session world, where that stream is what lets the world build.
    build(_regions("session", 50), using=alias, require_statistics=False)
    keys = {}
    for factor in (1, 3):
        with scaled_world(_regions("world", 2), factor, using=alias):
            keys[factor] = _pks(Region, alias, name="world")

    assert (len(keys[1]), len(keys[3])) == (2, 6)
    assert keys[1] <= keys[3]


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_world_inside_another_builds_beside_it_over_a_disjoint_table(alias: str) -> None:
    # The inner world builds beside the outer's rows, from its own stream, or keys collide.
    with scaled_world(_regions("outer", 2), 1, using=alias):
        outer = _pairs(Region, alias, "outer")
        with scaled_world(_regions("inner", 2), 1, using=alias) as rows:
            assert rows == 6
            assert _pairs(Region, alias, "outer") == outer
            assert len(_pks(Region, alias, name="inner")) == 2
        assert _pairs(Region, alias, "outer") == outer
        assert _pks(Region, alias, name="inner") == set()

    assert Region.objects.using(alias).count() == 0


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_world_whose_block_raised_leaves_the_next_one_its_keys(alias: str) -> None:
    # A world gives back its place however its block ends, or later keys would move.
    with scaled_world(_regions("world", 2), 1, using=alias):
        before = _pks(Region, alias)
    with pytest.raises(RuntimeError), scaled_world(_regions("world", 2), 1, using=alias):
        raise RuntimeError
    with scaled_world(_regions("world", 2), 1, using=alias):
        after = _pks(Region, alias)

    assert len(before) == 2
    assert after == before


class _Drawn:
    """UUID keys that note the stream each is drawn from, and claim nothing."""

    def __init__(self) -> None:
        self.streams: set[int] = set()

    def key_for(self, row: int, stream: int) -> object:
        self.streams.add(stream)
        return UuidKeys().key_for(row, stream)


class _Claimed(_Drawn):
    """The same keys, from a strategy implementing ``Disjoint`` and answering as told."""

    def __init__(self, disjoint: bool) -> None:
        super().__init__()
        self._disjoint = disjoint

    def is_disjoint_from_existing_rows(self) -> bool:
        return self._disjoint


def _one_table(keys: KeyStrategy, label: str = "world") -> Shape:
    return Shape(Table(Tenant, rows=2, keys=keys, name=Constant(label)), seed=5)


@pytest.mark.parametrize("alias", _ALIASES)
@pytest.mark.parametrize(
    ("strategy", "own_stream"),
    [
        (_Drawn, False),
        (partial(_Claimed, disjoint=False), False),
        (partial(_Claimed, disjoint=True), True),
    ],
    ids=["claims-nothing", "says-it-is-not", "says-it-is"],
)
def test_only_a_strategy_that_says_it_is_disjoint_draws_from_the_worlds_stream(
    alias: str, strategy: Callable[[], _Drawn], own_stream: bool
) -> None:
    # Everything else receives the stream build() gives it.
    in_world, in_build = strategy(), strategy()
    with scaled_world(_one_table(cast("KeyStrategy", in_world)), 1, using=alias):
        ...
    build(_one_table(cast("KeyStrategy", in_build)), using=alias, require_statistics=False)

    assert len(in_world.streams) == len(in_build.streams) == 1
    assert (in_world.streams != in_build.streams) is own_stream


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_strategy_that_says_it_is_not_disjoint_is_emptied(alias: str) -> None:
    # The world asked whether the strategy implements Disjoint; the build asks what it answers.
    tenant = Tenant.objects.using(alias).create(name="caller")

    try:
        with scaled_world(_one_table(_Claimed(disjoint=False)), 1, using=alias) as rows:
            assert rows == 2
            assert list(Tenant.objects.using(alias).values_list("name", flat=True)) == [
                "world",
                "world",
            ]
    except Exception as error:
        pytest.fail(f"{type(error).__name__}: {error}")

    assert list(Tenant.objects.using(alias).values_list("pk", "name")) == [(tenant.pk, "caller")]


@pytest.mark.parametrize("alias", _ALIASES)
def test_disjoint_keys_are_offered_for_a_strategy_that_says_it_is_not_disjoint(
    alias: str,
) -> None:
    # Emptied like any table whose keys can collide, so a refusal offers UuidKeys.
    tenant = Tenant.objects.using(alias).create(name="caller")
    TenantRecord.objects.using(alias).create(tenant=tenant, label="caller")

    with pytest.raises(Exception) as refused, contextlib.ExitStack() as entering:
        entering.enter_context(scaled_world(_one_table(_Claimed(disjoint=False)), 1, using=alias))

    assert refused.type is django_data_shape.ShapeReferenced
    assert "give testapp_tenant Disjoint keys (UuidKeys or Md5Keys)" in str(refused.value)
