from __future__ import annotations

import pytest

from django_data_shape import (
    Constant,
    Derived,
    FanOut,
    InvalidShape,
    Paired,
    Shape,
    Table,
    Uniform,
    Zipf,
    build,
)
from tests.testapp.models import Company, Tenant, TenantGrant, TenantRecord

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
    """A key that crossed a process boundary is still the key."""
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
    # Once both are read as the key they are, the parent would weigh twice.
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
    # Without the coercion the driver refused it, naming neither the table nor the field.
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


def test_a_parent_named_by_its_string_carries_its_values_to_a_derivation() -> None:
    # The narrowing must reach the ORM route too, so named keys bring their values back.
    tenants = [Tenant.objects.create(name=f"t{index}") for index in range(3)]
    named = [str(tenants[2].pk), str(tenants[0].pk)]

    build(
        Shape(
            Table(
                TenantRecord,
                rows=20,
                tenant=FanOut(Uniform(1, 2), parents=named),
                label=Derived("tenant.name", compute=str, scope="parent"),
            ),
            seed=3,
        ),
        require_statistics=False,
    )

    records = list(TenantRecord.objects.select_related("tenant"))
    assert {record.tenant_id for record in records} == {tenants[2].pk, tenants[0].pk}
    assert all(record.label == record.tenant.name for record in records)


def test_a_uuid_partner_named_by_its_string_is_that_partner() -> None:
    """``Paired(parents=)`` over a UUID-keyed partner, on both backends."""
    Company.objects.create(name="c0")
    Company.objects.create(name="c1")
    tenants = [Tenant.objects.create(name=f"t{index}") for index in range(4)]
    named = [str(tenant.pk) for tenant in tenants[1:]]

    build(
        Shape(
            Table(
                TenantGrant,
                rows=4,
                company=FanOut(Constant(1)),
                tenant=Paired("company", Zipf(), parents=named),
                role=Constant("r"),
            )
        ),
        require_statistics=False,
    )

    assert TenantGrant.objects.count() == 4
    assert set(TenantGrant.objects.values_list("tenant_id", flat=True)) <= {
        tenant.pk for tenant in tenants[1:]
    }
