"""A scaled world over Disjoint tables: what it empties, and the keys it builds.

A table whose keys are ``Disjoint`` is exempt from emptying, because its keys
cannot collide with rows already there. That exemption used to hold even when
the table referenced a declared table the world was emptying, so its rows were
left pointing at parents about to be removed -- and the world, finding a
reference from a table it was not emptying, refused its own declaration with
``ShapeReferenced``, naming a table the shape declares. That was the same-graph
composition the pytest page promises needs no arrangement, broken by nothing
more unusual than a UUID-keyed child.

Such a table is now emptied too, and so is one reaching the set through another,
because its rows are declared rows and cannot outlive the parents they point at.
One that references nothing being emptied keeps its rows, which is what the
exemption is for.

**And the world builds beside those rows with keys of its own.** A Disjoint key
is a digest of the seed and the row, so over a session world built from the
same shape a world used to make the session's keys again and fail on the
primary key. Every Disjoint table a world builds now draws its keys from a
stream distinct from the one ``build()`` uses for the same seed and table --
the same stream at every factor, and another for a world opened inside a world.
"Disjoint" is what the strategy answers, not whether it implements the
protocol: a strategy answering no is emptied and keeps the stream ``build()``
gives it, as the build refuses it for holding rows.

Every test runs on both aliases, because the set of tables to empty is decided
before either route's statements are chosen. The refusal is caught as
``Exception`` and its type checked by name, so this module still collects on a
tree without the fix and fails on its assertions there.
"""

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
    """Template, event and a UUID-keyed session, as one application declares them.

    With ``notes``, a UUID-keyed note on each session as well, which reaches the
    events only through the sessions.
    """
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
    # The defect: the sessions were left alone for their keys, and their
    # references into the events the world was emptying refused the world.
    build(_graph("session"), using=alias, require_statistics=False)
    session_world = _rows(alias)

    try:
        with scaled_world(_graph("world"), 1, using=alias) as rows:
            assert rows == 6
            # Only the world's own rows, the sessions included: none of the
            # session world's sessions survives the events it pointed at.
            assert _labels(alias) == [["world"], ["world"] * 2, ["world"] * 3, []]
    except Exception as error:
        pytest.fail(f"{type(error).__name__}: {error}")

    assert _rows(alias) == session_world


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_disjoint_table_reached_through_another_joins_too(alias: str) -> None:
    # The notes point at sessions, not at events, so they are in the set only
    # once the sessions are. One round of joining would leave them out, and
    # their references into the sessions would refuse the world.
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
    # The sessions hold no rows, so there is nothing of theirs to empty -- and
    # the caller's note, which points at no session, is reached from the events
    # only through them. It keeps its row, beside the world's notes.
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
    # The exemption itself, and what a consumer leans on when a parent is to be
    # kept: the tenants are UUID-keyed and point at nothing the world empties,
    # so they stay, and only the records -- which point at them, the other way
    # round -- are emptied.
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
        # The world's records only. Which tenants they point at is the fan-out's
        # business, and it reads every tenant the table holds -- the caller's
        # one included, which is the hybrid Disjoint keys exist to allow.
        assert (
            list(TenantRecord.objects.using(alias).values_list("label", flat=True)) == ["world"] * 4
        )

    assert list(Tenant.objects.using(alias).values_list("pk", "name")) == [(tenant.pk, "caller")]
    assert list(TenantRecord.objects.using(alias).values_list("tenant_id", "label")) == [
        (tenant.pk, "caller")
    ]


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_key_the_database_does_not_enforce_still_joins_a_disjoint_table(alias: str) -> None:
    # The threads' only key into the companies is db_constraint=False, and the
    # join reads the models' keys rather than the database's, so the threads are
    # emptied with the companies -- and the caller's remark on one is refused.
    # By the database's keys alone there would be nothing of the threads' to
    # empty and nothing to refuse, which is the refusal the docs warn this adds.
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
    # The tenants point at nothing the world empties, so they keep the
    # session's rows and the world builds beside them. With the session's seed
    # its keys were the session's, and the build failed on the primary key.
    build(_tenants("session", 50), using=alias, require_statistics=False)
    tenants, records = _pairs(Tenant, alias, "session"), _pairs(TenantRecord, alias, "session")

    with scaled_world(_tenants("world", 2), 1, using=alias) as rows:
        assert rows == 10
        # The session's tenants, untouched, and the world's two beside them.
        assert _pairs(Tenant, alias, "session") == tenants
        assert len(_pks(Tenant, alias, name="world")) == 2
        # The records have integer keys, so they were emptied, and the world's
        # point at tenants the table holds -- the session's included, since a
        # fan-out reads every parent there. Checked here because Django's keys
        # are deferred and the block is rolled back before any check would run.
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
    # Every table is Disjoint and none points into anything emptied, so the
    # world empties nothing at all and builds both tables beside the session's.
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
    # What the docs say a different seed buys: the build goes ahead, and the
    # tenants it builds sit beside the session's -- so the fan-out into them
    # reads both, and the world's records point at session tenants too.
    build(_tenants("session", 50, seed=6), using=alias, require_statistics=False)

    with scaled_world(_tenants("world", 2), 1, using=alias) as rows:
        assert rows == 10
        assert Tenant.objects.using(alias).count() == 52
        assert TenantRecord.objects.using(alias).filter(label="session").count() == 0
        assert TenantRecord.objects.using(alias).filter(tenant__name="session").exists()


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_worlds_children_point_at_its_own_disjoint_keys(alias: str) -> None:
    # A child reads its parents' keys from the table rather than computing
    # them, so drawing the parents' keys from another stream needs nothing of
    # the children. Over empty tables, so every depot can only point at a
    # region of the world's -- and those are not the regions build() makes for
    # the same seed, which is what the world's own stream means.
    with scaled_world(_regions("world", 2), 1, using=alias):
        regions = _pks(Region, alias)
        pointed_at = {region for _, _, region in _pairs(Depot, alias, "world")}
    build(_regions("built", 2), using=alias, require_statistics=False)

    assert len(regions) == 2
    assert pointed_at and pointed_at <= regions
    assert not regions & _pks(Region, alias)


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_worlds_disjoint_keys_are_the_same_at_every_factor(alias: str) -> None:
    # The stream a world draws from does not depend on the factor, so row i
    # has one key at every size, the way an integer key is i + 1 at every size.
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
    # The inner world leaves the regions alone, as the outer one did, so it
    # builds beside the outer world's rows -- from a stream that is its own
    # too, or it would make the outer world's keys and fail on them.
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
    # A world counts the worlds open around it to pick its stream, and gives
    # its place back however its block ends: otherwise every world after one
    # whose block raised would draw as if nested, and its keys would move.
    with scaled_world(_regions("world", 2), 1, using=alias):
        before = _pks(Region, alias)
    with pytest.raises(RuntimeError), scaled_world(_regions("world", 2), 1, using=alias):
        raise RuntimeError
    with scaled_world(_regions("world", 2), 1, using=alias):
        after = _pks(Region, alias)

    assert len(before) == 2
    assert after == before


class _Drawn:
    """UUID keys that note the stream each is drawn from, and claim nothing.

    Like :class:`~django_data_shape.keys.key_function.KeyFunction` in what
    matters here: it does not implement ``Disjoint``, so it is read as a
    strategy whose keys can collide.
    """

    def __init__(self) -> None:
        self.streams: set[int] = set()

    def key_for(self, row: int, stream: int) -> object:
        self.streams.add(stream)
        return UuidKeys().key_for(row, stream)


class _Claimed(_Drawn):
    """The same keys, from a strategy implementing ``Disjoint`` and answering as told.

    A third party's strategy can implement the protocol and say no, and
    ``build`` reads that answer: only a strategy that says yes is built beside
    rows already there.
    """

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
    # Everything else receives the stream build() gives it. The world's own
    # stream is there to keep a Disjoint table's keys off the rows it leaves in
    # place, and a table that is not Disjoint leaves none: it is emptied.
    in_world, in_build = strategy(), strategy()
    with scaled_world(_one_table(cast("KeyStrategy", in_world)), 1, using=alias):
        ...
    build(_one_table(cast("KeyStrategy", in_build)), using=alias, require_statistics=False)

    assert len(in_world.streams) == len(in_build.streams) == 1
    assert (in_world.streams != in_build.streams) is own_stream


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_strategy_that_says_it_is_not_disjoint_is_emptied(alias: str) -> None:
    # The world asked only whether the strategy implements Disjoint, while the
    # build asks what it answers -- so a strategy saying no was left in place
    # by the one and refused by the other, with ShapeNotEmpty.
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
    # Emptied like any table whose keys can collide, so a record the caller
    # made on its tenant refuses the world -- and UuidKeys would take the
    # table out of the emptying, so the refusal offers them.
    tenant = Tenant.objects.using(alias).create(name="caller")
    TenantRecord.objects.using(alias).create(tenant=tenant, label="caller")

    with pytest.raises(Exception) as refused, contextlib.ExitStack() as entering:
        entering.enter_context(scaled_world(_one_table(_Claimed(disjoint=False)), 1, using=alias))

    assert refused.type is django_data_shape.ShapeReferenced
    assert "give testapp_tenant Disjoint keys (UuidKeys or Md5Keys)" in str(refused.value)
