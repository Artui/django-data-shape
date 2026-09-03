"""Which table loads first, and why it has to be decided at all."""

from __future__ import annotations

import pytest

from django_data_shape import Constant, FanOut, InvalidShape, Projection, Table, Uniform, Zipf
from django_data_shape.order_tables import order_tables
from tests.testapp.models import (
    Attendance,
    Company,
    Event,
    EventSession,
    Left,
    Right,
    Session,
    Template,
    TemplateSession,
)


def test_a_parent_is_ordered_before_its_child() -> None:
    child = Table(Session, rows=10, company=FanOut(Zipf()), label=Constant("s"))
    parent = Table(Company, rows=5, name=Constant("acme"))

    # Declared child-first, loaded parent-first. Not because the database
    # insists -- Django's foreign keys are deferred, so any order commits -- but
    # because a fan-out reads the parent's real keys.
    assert [t.model for t in order_tables((child, parent))] == [Company, Session]


def test_an_order_that_is_already_correct_is_left_alone() -> None:
    parent = Table(Company, rows=5, name=Constant("acme"))
    child = Table(Session, rows=10, company=FanOut(Zipf()), label=Constant("s"))

    assert [t.model for t in order_tables((parent, child))] == [Company, Session]


def test_a_parent_outside_the_shape_is_not_ordered_against() -> None:
    # The supported hybrid: the parent was built by the ORM because its row
    # count is small, and only the child is declared here. Its keys get read at
    # resolve time like any other parent's.
    child = Table(Session, rows=10, company=FanOut(Zipf()), label=Constant("s"))

    assert [t.model for t in order_tables((child,))] == [Session]


def test_two_tables_fanning_out_over_each_other_are_refused_by_name() -> None:
    left = Table(Left, rows=5, right=FanOut(Uniform(1, 2)))
    right = Table(Right, rows=5, left=FanOut(Uniform(1, 2)))

    with pytest.raises(InvalidShape, match="cycle") as raised:
        order_tables((left, right))

    message = str(raised.value)
    assert "Left" in message and "Right" in message


def test_a_projection_is_ordered_after_both_tables_it_reads() -> None:
    projection = Projection(EventSession, per=Event, copying=TemplateSession)
    events = Table(Event, rows=5, template=FanOut(Zipf()), name=Constant("e"))
    sessions = Table(
        TemplateSession, rows=5, template=FanOut(Zipf()), title=Constant("s"), minutes=Constant(1)
    )
    templates = Table(Template, rows=2, name=Constant("t"))

    ordered = [t.model for t in order_tables((projection, events, sessions, templates))]

    assert ordered.index(EventSession) > ordered.index(Event)
    assert ordered.index(EventSession) > ordered.index(TemplateSession)


def test_a_projected_table_is_ordered_before_a_table_fanning_out_over_it() -> None:
    # The question a projection raises about load order, answered rather than
    # refused: running every projection last would forbid this by scheduling
    # accident rather than by anything true about the data.
    projection = Projection(EventSession, per=Event, copying=TemplateSession)
    attendance = Table(Attendance, rows=5, session=FanOut(Zipf()), name=Constant("a"))

    ordered = [t.model for t in order_tables((attendance, projection))]

    assert ordered == [EventSession, Attendance]


def test_a_projection_whose_inputs_are_outside_the_shape_is_not_ordered_against() -> None:
    # The same hybrid a fan-out supports: the tables it reads were built by the
    # ORM or by an earlier fixture, and nothing here has to order them.
    projection = Projection(EventSession, per=Event, copying=TemplateSession)

    assert [t.model for t in order_tables((projection,))] == [EventSession]


def test_a_statement_this_package_did_not_write_is_ordered_after_everything() -> None:
    # Nothing here parses SQL, so a raw projection names nothing it reads. The
    # only safe reading of "this could select from anything" is to run it last.
    opaque = Projection(EventSession, columns=("id",), sql="SELECT 1")
    company = Table(Company, rows=5, name=Constant("acme"))

    assert [t.model for t in order_tables((opaque, company))] == [Company, EventSession]


def test_two_raw_projections_keep_the_order_they_were_declared_in() -> None:
    # They have no edge between them, and inventing one would make two
    # independent escape hatches a cycle.
    first = Projection(EventSession, columns=("id",), sql="SELECT 1")
    second = Projection(Session, columns=("id",), sql="SELECT 2")

    assert [t.model for t in order_tables((first, second))] == [EventSession, Session]


def test_a_projection_and_a_table_reading_each_other_are_refused_by_name() -> None:
    projection = Projection(Left, per=Right, copying=Right)
    right = Table(Right, rows=5, left=FanOut(Uniform(1, 2)))

    with pytest.raises(InvalidShape, match="cycle") as raised:
        order_tables((projection, right))

    message = str(raised.value)
    assert "Left" in message and "Right" in message


def test_a_table_fanning_out_over_a_raw_projection_is_not_a_cycle() -> None:
    # The refusal that was simply wrong. A raw projection used to carry an edge
    # to every other declaration, so a table fanning out over one met its own
    # edge coming back and was told "A -> B -> A" about a chain -- with no
    # after= anywhere to correct it with. "Run it last" is a preference, and a
    # preference cannot contradict a declared edge; an edge can, and did.
    opaque = Projection(EventSession, columns=("id",), sql="SELECT 1")
    attendance = Table(Attendance, rows=5, session=FanOut(Zipf()), name=Constant("a"))

    assert [t.model for t in order_tables((attendance, opaque))] == [EventSession, Attendance]


def test_a_raw_projection_that_names_what_it_reads_is_ordered_after_it() -> None:
    # And the reason reads= is needed rather than nice: once the preference can
    # be overridden by a dependent, the statement may run before a table it
    # selects from. Naming it puts the edge back.
    opaque = Projection(
        EventSession,
        columns=("id",),
        sql="SELECT 1",
        reads=(Event,),
    )
    events = Table(Event, rows=5, template=FanOut(Zipf()), name=Constant("e"))
    attendance = Table(Attendance, rows=5, session=FanOut(Zipf()), name=Constant("a"))

    ordered = [t.model for t in order_tables((attendance, opaque, events))]

    assert ordered == [Event, EventSession, Attendance]


def test_two_raw_projections_with_a_dependent_each_are_still_not_a_cycle() -> None:
    # The sharper version of the same arrangement, and the one the old edges
    # could not express at all: two opaque statements, each needed by a
    # different table, so "run each of them after every table" was two
    # preferences that contradicted each other as well as the declared edges.
    # There is an order satisfying every declared edge, and only the preferences
    # were unsatisfiable together -- which is what makes them preferences.
    sessions = Projection(EventSession, columns=("id",), sql="SELECT 1")
    rights = Projection(Right, columns=("id",), sql="SELECT 2")
    attendance = Table(Attendance, rows=5, session=FanOut(Zipf()), name=Constant("a"))
    left = Table(Left, rows=5, right=FanOut(Uniform(1, 2)))

    ordered = [t.model for t in order_tables((attendance, sessions, left, rights))]

    assert ordered.index(EventSession) < ordered.index(Attendance)
    assert ordered.index(Right) < ordered.index(Left)
