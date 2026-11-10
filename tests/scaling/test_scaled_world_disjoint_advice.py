from __future__ import annotations

import contextlib

import pytest
from django.db import connections
from django.test.utils import CaptureQueriesContext

import django_data_shape
from django_data_shape import (
    Constant,
    FanOut,
    KeyFunction,
    KeyStrategy,
    SequentialKeys,
    Shape,
    Table,
    UuidKeys,
    Zipf,
    scaled_world,
)
from tests.testapp.models import (
    Event,
    Remark,
    SessionNote,
    StrictCode,
    StrictCodeField,
    Template,
    Tenant,
    TenantProfile,
    Thread,
    UuidSession,
)

pytestmark = pytest.mark.django_db(databases=["default", "not_postgres"])

_ALIASES = ["default", "not_postgres"]


def _refusal(shape: Shape, alias: str) -> str:
    """The ``ShapeReferenced`` message entering a world over ``shape`` raises."""
    with pytest.raises(Exception) as refused, contextlib.ExitStack() as entering:
        entering.enter_context(scaled_world(shape, 1, using=alias))
    assert refused.type is django_data_shape.ShapeReferenced, refused.value
    return " ".join(str(refused.value).split())


def _sessions_under_events(keys: KeyStrategy) -> Shape:
    return Shape(
        Table(Template, rows=1, name=Constant("world")),
        Table(Event, rows=2, template=FanOut(Zipf()), name=Constant("world")),
        Table(UuidSession, rows=3, keys=keys, event=FanOut(Zipf()), title=Constant("world")),
        seed=5,
    )


@pytest.mark.parametrize("alias", _ALIASES)
def test_disjoint_keys_are_not_offered_where_the_table_would_be_emptied_all_the_same(
    alias: str,
) -> None:
    # Which is what following the advice would have shown: the same refusal.
    template = Template.objects.using(alias).create(name="caller")
    event = Event.objects.using(alias).create(template=template, name="caller")
    session = UuidSession.objects.using(alias).create(event=event, title="caller")
    SessionNote.objects.using(alias).create(session=session, text="caller")

    message = _refusal(_sessions_under_events(SequentialKeys()), alias)

    assert "(testapp_sessionnote.session_id -> testapp_uuidsession)" in message
    assert "Disjoint" not in message
    assert _refusal(_sessions_under_events(UuidKeys()), alias) == message


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_key_into_itself_does_not_withhold_disjoint_keys(alias: str) -> None:
    # The advice is offered, and following it builds.
    thread = Thread.objects.using(alias).create(title="caller")
    Remark.objects.using(alias).create(thread=thread, text="caller")

    def threads(keys: KeyStrategy) -> Shape:
        return Shape(Table(Thread, rows=2, keys=keys, title=Constant("world")), seed=5)

    message = _refusal(threads(SequentialKeys()), alias)

    assert "(testapp_remark.thread_id -> testapp_thread)" in message
    assert "give testapp_thread Disjoint keys (UuidKeys or Md5Keys)" in message
    with scaled_world(threads(UuidKeys()), 1, using=alias) as rows:
        assert rows == 2


@pytest.mark.parametrize("alias", _ALIASES)
def test_disjoint_keys_are_not_offered_for_a_key_that_is_a_relation(alias: str) -> None:
    # A UUID fits a one-to-one key into the tenants, so no Disjoint key is a tenant's.
    tenant = Tenant.objects.using(alias).create(name="caller")
    profile = TenantProfile.objects.using(alias).create(tenant=tenant, text="caller")
    Remark.objects.using(alias).create(profile=profile, text="caller")
    shape = Shape(
        Table(TenantProfile, rows=1, keys=SequentialKeys(), text=Constant("world")), seed=5
    )

    with (
        CaptureQueriesContext(connections["default"]) as on_default,
        CaptureQueriesContext(connections["not_postgres"]) as on_not_postgres,
    ):
        message = _refusal(shape, alias)

    assert "(testapp_remark.profile_id -> testapp_tenantprofile)" in message
    assert "Disjoint" not in message
    statements = [query["sql"] for query in [*on_default, *on_not_postgres]]
    assert not [sql for sql in statements if 'FROM "testapp_tenant"' in sql]


@pytest.mark.parametrize("alias", _ALIASES)
@pytest.mark.parametrize("refusal", [ValueError, TypeError])
def test_a_key_field_refusing_outside_its_contract_still_refuses_the_world(
    alias: str, refusal: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A field raising a non-ValidationError used to escape instead of the refusal.
    monkeypatch.setattr(StrictCodeField, "refusal", refusal)
    code = StrictCode.objects.using(alias).create(code="code-caller", name="caller")
    Remark.objects.using(alias).create(code=code, text="caller")
    shape = Shape(
        Table(
            StrictCode, rows=2, keys=KeyFunction(lambda row: f"code-{row}"), name=Constant("world")
        ),
        seed=5,
    )

    message = _refusal(shape, alias)

    assert "(testapp_remark.code_id -> testapp_strictcode)" in message
    assert "Disjoint" not in message
