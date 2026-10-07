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

import pytest

from django_data_shape import Constant, FanOut, Shape, Table, Zipf, build, scaled_world
from tests.testapp.models import Event, SessionNote, Template, Tenant, TenantRecord, UuidSession

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
