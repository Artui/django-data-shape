"""Keys named in ``parents=`` in a spelling other than the primary key's own.

Portable on purpose, unlike the graph tests beside the loader: the two backends
fail this differently. PostgreSQL casts a string in the predicate and returns a
``uuid.UUID`` that compared unequal to it; SQLite has no binding for a UUID
parameter at all and stores the key as hex, so a UUID-keyed parent could not be
narrowed there in either spelling.
"""

from __future__ import annotations

import pytest

from django_data_shape import Constant, FanOut, InvalidShape, Shape, Table, build
from tests.testapp.models import Tenant, TenantRecord

pytestmark = pytest.mark.django_db(transaction=True)


def test_a_uuid_parent_named_as_itself_is_that_parent() -> None:
    ours = Tenant.objects.create(name="ours")

    build(
        Shape(
            Table(
                TenantRecord,
                rows=10,
                tenant=FanOut(Constant(1), parents=[ours.pk]),
                label=Constant("s"),
            )
        ),
        require_statistics=False,
    )

    assert set(TenantRecord.objects.values_list("tenant_id", flat=True)) == {ours.pk}


def test_a_uuid_parent_named_by_its_string_is_that_parent() -> None:
    """A key that crossed a process boundary is still the key.

    Ids loaded from JSON or handed between processes are strings. PostgreSQL
    casts the string in the predicate, so the row comes back -- as a
    ``uuid.UUID`` -- and comparing it with the string named it was reported as
    a row that does not exist, pointing the reader at a rollback that never
    happened.
    """
    ours = Tenant.objects.create(name="ours")

    build(
        Shape(
            Table(
                TenantRecord,
                rows=10,
                tenant=FanOut(Constant(1), parents=[str(ours.pk)]),
                label=Constant("s"),
            ),
            seed=1,
        ),
        require_statistics=False,
    )

    assert set(TenantRecord.objects.values_list("tenant_id", flat=True)) == {ours.pk}


def test_one_parent_named_in_two_spellings_is_refused_as_a_repeat() -> None:
    # The constructor refuses a repeated key, but it compares what it was
    # given, and a string and a UUID are not equal there. Once both are read
    # as the key they are, the parent would weigh twice.
    ours = Tenant.objects.create(name="ours")

    with pytest.raises(InvalidShape, match="more than once") as raised:
        build(
            Shape(
                Table(
                    TenantRecord,
                    rows=10,
                    tenant=FanOut(Constant(1), parents=[str(ours.pk), ours.pk]),
                    label=Constant("s"),
                )
            ),
            require_statistics=False,
        )

    assert "not a row" not in str(raised.value)


def test_a_value_that_cannot_be_the_parents_key_is_refused_by_name() -> None:
    # Without the coercion this reached the driver, which refused it in its own
    # words and named neither the table nor the field.
    Tenant.objects.create(name="ours")

    with pytest.raises(InvalidShape, match="'not-a-uuid'") as raised:
        build(
            Shape(
                Table(
                    TenantRecord,
                    rows=10,
                    tenant=FanOut(Constant(1), parents=["not-a-uuid"]),
                    label=Constant("s"),
                )
            ),
            require_statistics=False,
        )

    message = str(raised.value)
    assert "testapp_tenantrecord.tenant" in message
    assert "not a row" not in message
