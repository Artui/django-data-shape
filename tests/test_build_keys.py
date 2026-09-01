"""Loading tables whose primary keys are not integers."""

from __future__ import annotations

import datetime
import uuid

import pytest
from django.db import connection

from django_data_shape import (
    Constant,
    FanOut,
    KeyFunction,
    Shape,
    Table,
    Uniform,
)
from django_data_shape import build as build_shape
from tests.testapp.models import Reading, SlugPk, Tenant, TenantRecord

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="COPY loading and planner statistics need PostgreSQL",
    ),
]


def test_a_uuid_keyed_table_loads() -> None:
    # Refused outright before this release, which made the package unusable for
    # any project on UUID keys.
    build_shape(Shape(Table(Tenant, rows=500, name=Constant("acme"))))

    keys = list(Tenant.objects.values_list("id", flat=True))
    assert len(keys) == 500
    assert len(set(keys)) == 500
    assert all(isinstance(key, uuid.UUID) and key.version == 4 for key in keys)


def test_a_foreign_key_over_a_uuid_parent_carries_uuids() -> None:
    # The case that proves the work reaches past the key column: the parent's
    # real keys come back as UUIDs and have to survive into the child's column.
    build_shape(
        Shape(
            Table(Tenant, rows=40, name=Constant("acme")),
            Table(
                TenantRecord,
                rows=4000,
                tenant=FanOut(Uniform(1, 6)),
                label=Constant("r"),
            ),
        )
    )

    parents = set(Tenant.objects.values_list("id", flat=True))
    children = set(TenantRecord.objects.values_list("tenant_id", flat=True))

    assert TenantRecord.objects.count() == 4000
    assert children <= parents
    assert all(isinstance(key, uuid.UUID) for key in children)


def test_uuid_keys_are_reproducible_across_builds() -> None:
    shape = Shape(Table(Tenant, rows=200, name=Constant("acme")), seed=99)

    build_shape(shape)
    first = sorted(Tenant.objects.values_list("id", flat=True))
    Tenant.objects.all().delete()
    build_shape(shape)
    second = sorted(Tenant.objects.values_list("id", flat=True))

    # Derived, not drawn. A random key would make every foreign key pointing at
    # it differ between two builds of one shape.
    assert first == second


def test_an_exotic_key_loads_when_the_caller_declares_it() -> None:
    build_shape(
        Shape(
            Table(
                SlugPk,
                rows=250,
                name=Constant("x"),
                keys=KeyFunction(lambda row: f"page-{row:05d}"),
            )
        )
    )

    codes = sorted(SlugPk.objects.values_list("code", flat=True))
    assert len(codes) == 250
    assert codes[0] == "page-00000"
    assert codes[-1] == "page-00249"


def test_a_uuid_table_needs_no_sequence_reset_and_still_accepts_orm_writes() -> None:
    build_shape(Shape(Table(Tenant, rows=50, name=Constant("acme"))))

    # There is no sequence behind a UUID key, so nothing to move -- but the
    # first ORM write still has to work, which is the failure the reset exists
    # to prevent on integer keys.
    made = Tenant.objects.create(name="after")

    assert Tenant.objects.count() == 51
    assert made.pk not in set(Tenant.objects.exclude(pk=made.pk).values_list("id", flat=True))


def test_an_exotic_key_is_prepared_by_its_field_like_any_other_value() -> None:
    # The primary key used to go straight to the driver while every other value
    # went through Django's field preparation. For an integer that is the same
    # thing and for a UUID psycopg happens to adapt it -- but a naive datetime
    # is stored verbatim rather than localised, which is exactly the bug already
    # found once on an ordinary column.
    base = datetime.datetime(2020, 1, 1, 12, 0)
    with pytest.warns(RuntimeWarning, match="received a naive datetime"):
        build_shape(
            Shape(
                Table(
                    Reading,
                    rows=3,
                    value=Constant(1),
                    keys=KeyFunction(lambda row: base + datetime.timedelta(hours=row)),
                )
            )
        )

    stored = sorted(Reading.objects.values_list("at", flat=True))

    # 12:00 in the suite's America/Chicago TIME_ZONE is 18:00 UTC. Without the
    # field's preparation it would have been stored as 12:00 UTC.
    assert stored[0] == datetime.datetime(2020, 1, 1, 18, 0, tzinfo=datetime.timezone.utc)
