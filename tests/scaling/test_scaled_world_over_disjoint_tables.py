"""A declared table with Disjoint keys is emptied when it points into what is.

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

Every test runs on both aliases, because the set of tables to empty is decided
before either route's statements are chosen. The refusal is caught as
``Exception`` and its type checked by name, so this module still collects on a
tree without the fix and fails on its assertions there.
"""

from __future__ import annotations

import contextlib

import pytest
from django.db import IntegrityError

import django_data_shape
from django_data_shape import Constant, FanOut, Shape, Table, Zipf, build, scaled_world
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


# A Disjoint table pointing at nothing the world empties keeps its rows, and over
# a session world declaring it those rows are the session's. Its keys are a
# digest of the row and of the seed, which scaling keeps, so with the session's
# seed the world makes the session's keys and the build collides with its rows.
# Strict, so that whatever ends the collision has to remove the mark and say
# what a world over such a table now sees.
_SAME_SEED_COLLIDES = pytest.mark.xfail(
    strict=True,
    raises=IntegrityError,
    reason="a Disjoint table nothing emptied points into keeps the session's rows",
)


@_SAME_SEED_COLLIDES
@pytest.mark.parametrize("alias", _ALIASES)
def test_a_session_world_with_a_disjoint_root_sits_under_the_same_graph(alias: str) -> None:
    build(_tenants("session", 50), using=alias, require_statistics=False)
    session_tenants = sorted(Tenant.objects.using(alias).values_list("pk", flat=True), key=str)

    with scaled_world(_tenants("world", 2), 1, using=alias) as rows:
        assert rows == 10

    assert (
        sorted(Tenant.objects.using(alias).values_list("pk", flat=True), key=str) == session_tenants
    )


@_SAME_SEED_COLLIDES
@pytest.mark.parametrize("alias", _ALIASES)
def test_a_session_world_keyed_by_uuids_throughout_sits_under_the_same_graph(alias: str) -> None:
    build(_regions("session", 50), using=alias, require_statistics=False)
    session_regions = sorted(Region.objects.using(alias).values_list("pk", flat=True), key=str)

    with scaled_world(_regions("world", 2), 1, using=alias) as rows:
        assert rows == 6

    assert (
        sorted(Region.objects.using(alias).values_list("pk", flat=True), key=str) == session_regions
    )


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
