"""A scaled world can be built over rows a session world already holds.

Both pytest surfaces this package ships want the same tables in an application
with one model graph, and the two used to be mutually exclusive: a session world
holds its rows for the whole run, so the scaled build met a table that was not
empty and refused. The documented answer -- give the two different models -- is
not available to a project whose plan assertions and growth assertions are about
the same flow, because that is what the application is.

The failure was also order-dependent, which is what made it worth fixing rather
than documenting: a suite whose growth tests happened to be collected first
passed, and the same tests named in the other order failed.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from django.db import connection

import django_data_shape
from django_data_shape import (
    Constant,
    FanOut,
    Shape,
    Table,
    UuidKeys,
    Zipf,
    build,
    scaled_world,
)
from tests.testapp.models import Company, Session, Tenant

pytestmark = pytest.mark.django_db


def _shape(rows: int = 4) -> Shape:
    return Shape(
        Table(Company, rows=rows, name=Constant("acme")),
        Table(Session, rows=rows * 2, company=FanOut(Zipf()), label=Constant("s")),
    )


def _session_world() -> Shape:
    """A separate declaration, as a session fixture's would be."""
    return Shape(Table(Company, rows=7, name=Constant("session")))


def test_a_scaled_world_builds_over_a_session_world() -> None:
    build(_session_world(), require_statistics=False)
    assert Company.objects.count() == 7

    with scaled_world(_shape(), 2, using=connection.alias) as rows:
        # Inside the block the world is exactly what was declared, at this
        # factor. That is what "make the world be at factor F" has to mean.
        assert rows == 8 + 16
        assert Company.objects.count() == 8
        assert not Company.objects.filter(name="session").exists()

    # And the session world is back, because the emptying happened inside the
    # transaction the scaled world rolls back. Nothing was snapshotted.
    assert Company.objects.count() == 7
    assert Company.objects.filter(name="session").count() == 7


def test_two_scaled_worlds_run_back_to_back() -> None:
    """The growth assertion's own shape: the same tables, twice, at two sizes."""
    for factor in (1, 10):
        with scaled_world(_shape(), factor, using=connection.alias) as rows:
            assert rows == 4 * factor + 8 * factor


def test_a_scaled_world_leaves_an_empty_database_empty() -> None:
    with scaled_world(_shape(), 1, using=connection.alias):
        assert Company.objects.count() == 4

    assert Company.objects.count() == 0


def test_a_shape_whose_tables_all_keep_their_own_keys_empties_nothing() -> None:
    """The `Disjoint` exemption, mirroring the one `build` already makes.

    A UUID key is a digest that cannot land on a caller's row, which is what
    lets the hybrid this package documents work: parents made by your code,
    children made here. Deleting those parents to make room would break that
    hybrid in a new way, so a shape declaring only such tables empties nothing.
    """
    Tenant.objects.create(name="made-by-the-caller")

    shape = Shape(Table(Tenant, rows=3, keys=UuidKeys(), name=Constant("built")))

    with scaled_world(shape, 1, using=connection.alias):
        # Four: the caller's row is still there, beside the three built ones.
        assert Tenant.objects.count() == 4
        assert Tenant.objects.filter(name="made-by-the-caller").exists()

    assert Tenant.objects.count() == 1


def test_a_session_table_the_scaled_shape_leaves_out_is_refused_not_emptied() -> None:
    """A scaled world over part of a session world's graph.

    The session world holds companies and their sessions; the scaled world
    declares companies alone. Emptying the companies would orphan the session
    rows or take them along, and the sessions are not the scaled world's, so it
    refuses -- naming the reference, which is the table to add to its shape.
    The pytest page quotes the start of this message, and is held to it here.
    """
    build(_shape(), require_statistics=False)

    with (
        pytest.raises(
            Exception, match=r"testapp_session\.company_id -> testapp_company"
        ) as refused,
        contextlib.ExitStack() as entering,
    ):
        entering.enter_context(
            scaled_world(Shape(Table(Company, rows=2, name=Constant("world"))), 1)
        )

    assert refused.type is django_data_shape.ShapeReferenced
    message = " ".join(str(refused.value).split())
    assert "(testapp_session.company_id -> testapp_company)" in message
    page = (Path(__file__).resolve().parents[2] / "docs" / "pytest.md").read_text()
    quoted = page.split("```text\nA scaled world cannot empty", 1)[1].split("...", 1)[0]
    assert message.startswith(" ".join(f"A scaled world cannot empty{quoted}".split()))
    # Nothing was removed on the way to the refusal.
    assert Company.objects.filter(name="acme").count() == 4
    assert Session.objects.count() == 8
