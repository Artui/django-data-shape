from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

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
        assert rows == 8 + 16
        assert Company.objects.count() == 8
        assert not Company.objects.filter(name="session").exists()

    # Nothing was snapshotted.
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
    """The `Disjoint` exemption, mirroring the one `build` already makes."""
    Tenant.objects.create(name="made-by-the-caller")

    shape = Shape(Table(Tenant, rows=3, keys=UuidKeys(), name=Constant("built")))

    with (
        CaptureQueriesContext(connection) as captured,
        scaled_world(shape, 1, using=connection.alias),
    ):
        assert Tenant.objects.count() == 4
        assert Tenant.objects.filter(name="made-by-the-caller").exists()

    assert Tenant.objects.count() == 1
    assert [query["sql"] for query in captured if "EXISTS" in query["sql"]] == []


def test_a_session_table_the_scaled_shape_leaves_out_is_refused_not_emptied() -> None:
    """A scaled world over part of a session world's graph."""
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
    assert Company.objects.filter(name="acme").count() == 4
    assert Session.objects.count() == 8
