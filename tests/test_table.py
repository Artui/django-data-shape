"""Every refusal Table makes, and why it makes it."""

from __future__ import annotations

import datetime
import operator
from typing import Any, cast

import django
import pytest
from django.db import models

from django_data_shape import (
    After,
    Aligned,
    Bounded,
    Constant,
    Derived,
    Distribution,
    FanOut,
    Given,
    InvalidShape,
    KeyFunction,
    Sequential,
    SequentialKeys,
    Skew,
    Table,
    Uniform,
    UuidKeys,
    Zipf,
)
from tests.testapp.models import (
    Company,
    Defaulted,
    Order,
    Project,
    Referred,
    Reserved,
    Session,
    SlugPk,
    Subscriber,
    Tenant,
    Ticket,
)


def _order(**overrides: Distribution) -> Table:
    fields: dict[str, Distribution] = {
        "status": Skew({"complete": 0.9, "pending": 0.1}),
        "total": Uniform(0, 500, places=2),
        "created_at": Sequential(0, 1),
    }
    fields.update(overrides)
    return Table(Order, rows=10, **fields)


def test_a_complete_declaration_is_accepted() -> None:
    table = _order()

    assert table.rows == 10
    assert table.db_table == Order._meta.db_table
    assert repr(table) == "Table(Order, rows=10)"


def test_columns_come_back_in_a_stable_order() -> None:
    # Declaration order must not reach the generated SQL: two shapes that differ
    # only in the order of two keyword arguments are the same shape, and will
    # need to hash to the same cache key when template reuse lands.
    names = [name for name, _ in _order().columns()]

    assert names == sorted(names)
    # channel is here without being declared: it carries a Python-level default,
    # which COPY would not apply, so it is filled in rather than omitted.
    assert names == ["channel", "created_at", "status", "total"]


def test_a_negative_row_count_is_refused() -> None:
    with pytest.raises(InvalidShape, match="cannot have -1 rows"):
        Table(Company, rows=-1, name=Constant("x"))


def test_zero_rows_is_allowed() -> None:
    # An empty table is a legitimate shape: it is what a parent with no children
    # looks like, and refusing it would make that case undeclarable.
    assert Table(Company, rows=0, name=Constant("x")).rows == 0


def test_declaring_a_field_twice_is_refused() -> None:
    with pytest.raises(InvalidShape, match="declares name twice"):
        Table(Company, rows=1, fields={"name": Constant("a")}, name=Constant("b"))


def test_an_unknown_field_is_refused_and_the_real_ones_listed() -> None:
    with pytest.raises(InvalidShape, match="no field named nickname") as raised:
        Table(Company, rows=1, name=Constant("x"), nickname=Constant("y"))

    assert "name" in str(raised.value)


def test_declaring_the_primary_key_is_refused() -> None:
    with pytest.raises(InvalidShape, match="is the primary key"):
        Table(Company, rows=1, name=Constant("x"), id=Sequential(1, 1))


def test_a_relation_needs_a_fanout_not_a_value_distribution() -> None:
    # A value distribution over a foreign key column emits keys drawn from
    # nothing, pointing at rows that may not exist -- the one thing referential
    # integrity by construction exists to make impossible.
    with pytest.raises(InvalidShape, match="needs a FanOut"):
        Table(
            Project,
            rows=1,
            company=Constant(1),
            status=Constant("ACTIVE"),
            created_at=Sequential(0, 1),
        )


def test_a_fanout_on_a_plain_column_is_refused() -> None:
    with pytest.raises(InvalidShape, match="nothing to fan out over"):
        Table(Company, rows=1, name=FanOut(Zipf()))


def test_a_relation_declared_with_a_fanout_is_accepted() -> None:
    table = Table(Session, rows=10, company=FanOut(Zipf()), label=Constant("s"))

    assert [name for name, _ in table.relations()] == ["company"]
    assert [name for name, _ in table.columns()] == ["company", "label"]


def test_a_required_field_left_out_is_refused_by_name() -> None:
    with pytest.raises(InvalidShape, match="created_at, status, total") as raised:
        Table(Order, rows=1)

    assert "has to be declared" in str(raised.value)


def test_a_nullable_field_is_left_out_and_becomes_null() -> None:
    assert "note" not in _order().fields


def test_a_python_default_is_filled_in_rather_than_omitted() -> None:
    # The trap this guards: a Django ``default=`` is applied by save(), and this
    # package never calls save(). The column is NOT NULL with nothing behind it
    # at the database level, so leaving it out of the COPY fails the load. It is
    # filled with exactly what save() would have written.
    channel = _order().fields["channel"]

    assert channel.value(0, 0.0) == "web"
    assert channel.value(999, 0.9) == "web"


def test_a_declared_distribution_wins_over_the_model_default() -> None:
    assert _order(channel=Constant("api")).fields["channel"].value(0, 0.0) == "api"


def test_a_callable_default_is_refused_rather_than_guessed() -> None:
    # uuid4 varies per row and dict does not, and this package cannot tell them
    # apart -- so it refuses instead of writing rows the application never would.
    with pytest.raises(InvalidShape, match="callable default"):
        Table(Defaulted, rows=1)


def test_a_field_colliding_with_the_signature_is_declarable_through_fields() -> None:
    # The escape hatch earning its keep: `rows` is an ordinary column name, and
    # Python binds the keyword to the row count before the field sees it.
    table = Table(Reserved, rows=3, fields={"rows": Sequential(0, 1)})

    assert [name for name, _ in table.columns()] == ["rows"]
    assert table.rows == 3


def test_a_key_type_with_no_obvious_strategy_is_refused_and_points_at_the_fix() -> None:
    # It used to load: the dense 1..N range went into the CharField verbatim and
    # wrote "1", "2", "3" -- values the application could never produce, with a
    # whole statistics picture built on top of them, and no error anywhere.
    with pytest.raises(InvalidShape, match="CharField primary") as raised:
        Table(SlugPk, rows=3, name=Constant("x"))

    # Refusing is only half of it. The message has to say what to do instead, or
    # the reader is left to discover keys= by reading the source.
    assert "keys=" in str(raised.value)
    assert "KeyFunction" in str(raised.value)


def test_an_integer_key_infers_a_counter() -> None:
    assert isinstance(_order().keys, SequentialKeys)


def test_a_uuid_key_infers_derived_uuids() -> None:
    # A UUID primary key used to be refused outright, which made this package
    # unusable for a whole class of project.
    assert isinstance(Table(Tenant, rows=5, name=Constant("t")).keys, UuidKeys)


def test_an_explicit_strategy_makes_an_exotic_key_declarable() -> None:
    table = Table(SlugPk, rows=3, name=Constant("x"), keys=KeyFunction(lambda row: f"s-{row}"))

    assert table.keys.key_for(2, 0) == "s-2"


def test_an_explicit_strategy_overrides_the_inferred_one() -> None:
    table = Table(Company, rows=3, name=Constant("x"), keys=KeyFunction(lambda row: row * 10))

    assert table.keys.key_for(2, 0) == 20


def test_omitting_a_required_relation_is_refused_like_declaring_one() -> None:
    # Declaring a relation was already refused; omitting a required one was not,
    # and reached COPY to die there on a not-null violation. Both directions
    # have to refuse, or the contract only holds for callers who tried the
    # unsupported thing explicitly.
    with pytest.raises(InvalidShape, match="relation that cannot be null"):
        Table(Project, rows=1, status=Constant("ACTIVE"), created_at=Sequential(0, 1))


def test_a_nullable_relation_may_be_omitted_and_loads_null() -> None:
    # Allowed rather than refused: optional foreign keys are common enough that
    # refusing them would make most real models unshapeable this release. The
    # column loads entirely NULL, which is stated in the documentation because a
    # join key with null_frac 1.0 is not a neutral thing to hand a planner.
    assert "referrer" not in Table(Referred, rows=1, label=Constant("x")).fields


def test_a_constant_cannot_fill_a_unique_column_twice() -> None:
    # Arithmetic, decidable here. It used to be discovered by the database
    # partway through a load that had already written most of a table.
    with pytest.raises(InvalidShape, match="needs 5 distinct values"):
        Table(Subscriber, rows=5, email=Constant("a@example.com"))


def test_a_skew_with_too_few_values_cannot_fill_a_unique_column() -> None:
    with pytest.raises(InvalidShape, match="can only produce 2"):
        Table(Subscriber, rows=5, email=Skew({"a@example.com": 1, "b@example.com": 1}))


def test_a_bounded_distribution_that_is_big_enough_is_accepted() -> None:
    assert Table(Subscriber, rows=1, email=Constant("a@example.com")).rows == 1


def test_an_unbounded_distribution_is_not_second_guessed() -> None:
    # Sequential does not implement Bounded, so it is treated as unbounded
    # rather than as suspicious. A distribution that cannot answer the question
    # should not be refused for failing to.
    assert not isinstance(Sequential(0, 1), Bounded)
    assert Table(Subscriber, rows=1000, email=Sequential(0, 1)).rows == 1000


def test_a_declaration_cannot_be_edited_past_its_own_validation() -> None:
    # Every rule in Table runs once, in __init__. While the attributes were
    # writable a declaration could be rewritten afterwards into one that would
    # have been refused, and nothing re-checked it.
    table = _order()

    for attribute, value in (("rows", -1), ("model", Company), ("fields", {})):
        with pytest.raises(AttributeError):
            setattr(table, attribute, value)

    with pytest.raises(TypeError):
        table.fields["status"] = Constant("x")


@pytest.mark.skipif(django.VERSION < (5, 2), reason="composite primary keys arrived in Django 5.2")
def test_a_composite_primary_key_is_refused_as_arity_not_type() -> None:
    # It used to raise a bare StopIteration from inside the package: a composite
    # key is not among the concrete fields, because it has no column of its own.
    # The message has to say keys= cannot help, or the reader will reasonably
    # try the escape hatch that works for every other unusual key.
    class Composite(models.Model):
        pk = models.CompositePrimaryKey("left_id", "right_id")
        left_id = models.IntegerField()
        right_id = models.IntegerField()

        class Meta:
            app_label = "testapp"

    with pytest.raises(InvalidShape, match="composite primary key") as raised:
        Table(Composite, rows=3, left_id=Constant(1), right_id=Constant(2))

    assert "arity, not type" in str(raised.value)


def _ticket(**overrides: object) -> Table:
    fields: dict[str, object] = {
        "account": FanOut(Zipf(1.2)),
        "opened_at": After("account.signed_up_at", within=datetime.timedelta(days=30)),
        "severity": Given("account.plan", {"free": Constant("low")}, default=Constant("high")),
        "quantity": Aligned("size", Uniform(1, 100, places=0)),
        "unit_price": Aligned("size", Uniform(1, 500, places=2)),
        "total": Derived("quantity", "unit_price", compute=operator.mul),
    }
    fields.update(overrides)
    return Table(Ticket, rows=10, fields=cast("Any", fields))


def test_all_four_faces_are_declarable_on_one_table() -> None:
    table = _ticket()

    # The point of the mechanism, asserted as one declaration rather than four:
    # within-row, across-the-parent twice, and a shared rank, all resolved by
    # the same validation and the same ordering.
    assert [name for name, _ in table.columns()] == [
        "account",
        "opened_at",
        "quantity",
        "severity",
        "total",
        "unit_price",
    ]


def test_computation_order_is_not_column_order() -> None:
    order = _ticket().computation_order()

    # Both are total orders over the same names and they disagree, which is the
    # whole reason there are two: sorted by name, total precedes unit_price.
    assert order.index("unit_price") < order.index("total")
    assert order.index("quantity") < order.index("total")


def test_a_row_source_that_is_not_a_declared_column_is_refused() -> None:
    with pytest.raises(InvalidShape, match="derived from margin, which is not declared"):
        _ticket(total=Derived("margin", compute=str))


def test_several_missing_row_sources_are_named_together() -> None:
    with pytest.raises(InvalidShape, match="derived from margin, tax, which are not declared"):
        _ticket(total=Derived("tax", "margin", compute=str))


def test_a_parent_source_has_to_name_the_relation_and_the_field() -> None:
    with pytest.raises(InvalidShape, match="'relation.field'"):
        _ticket(opened_at=Derived("signed_up_at", compute=str, scope="parent"))


def test_a_parent_source_over_an_undeclared_relation_is_refused() -> None:
    with pytest.raises(InvalidShape, match="customer is not a fan-out"):
        _ticket(opened_at=Derived("customer.signed_up_at", compute=str, scope="parent"))


def test_a_parent_source_over_a_nullable_fan_out_is_refused() -> None:
    # A child with no parent has no value to be after, and substituting one
    # would be the approximation this package refuses everywhere else. Caught
    # here rather than met as a None inside the arithmetic.
    with pytest.raises(InvalidShape, match="have no parent to read from"):
        _ticket(account=FanOut(Zipf(1.2), null=0.2))


def test_a_parent_field_the_parent_does_not_have_is_refused_and_the_real_ones_listed() -> None:
    with pytest.raises(InvalidShape, match="Account has no field named signup") as raised:
        _ticket(opened_at=After("account.signup", within=datetime.timedelta(days=1)))

    assert "plan, signed_up_at" in str(raised.value)


def test_a_derivation_on_a_relation_column_is_refused_like_a_distribution() -> None:
    with pytest.raises(InvalidShape, match="needs a FanOut"):
        _ticket(account=Derived("quantity", compute=str))


def test_a_cycle_among_derivations_is_refused_at_declaration_time() -> None:
    with pytest.raises(InvalidShape, match="in a cycle"):
        _ticket(
            total=Derived("quantity", compute=str),
            quantity=Derived("total", compute=str),
        )


def test_a_derivation_may_read_a_column_the_model_defaulted() -> None:
    # channel is never declared -- Table fills it in from the model's own
    # default -- and a derivation reading it has to be checked after that has
    # happened, or the source it names is not there to be found.
    table = Table(
        Order,
        rows=3,
        status=Constant("complete"),
        total=Constant(1),
        created_at=Sequential(0, 1),
        note=Derived("channel", compute=str),
    )

    assert table.computation_order() == ("note",)


def test_the_parent_columns_a_table_reads_are_reported_per_relation() -> None:
    # What the build turns into extra columns on the query that already reads
    # the parent's keys, which is how a child reaches across the edge without a
    # lookup of its own.
    assert _ticket().parent_fields() == {"account": ("plan", "signed_up_at")}


def test_a_table_with_no_parent_derivations_reads_nothing_extra() -> None:
    assert Table(Session, rows=5, company=FanOut(Zipf()), label=Constant("s")).parent_fields() == {}
