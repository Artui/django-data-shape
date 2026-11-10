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
    Paired,
    PerParent,
    Sequential,
    SequentialKeys,
    Skew,
    Table,
    Uniform,
    UuidKeys,
    Zipf,
)
from tests.testapp.models import (
    Assigned,
    Company,
    CompanyProxy,
    Defaulted,
    Delegated,
    DeliveryDocument,
    Membership,
    Memo,
    Order,
    Project,
    Referred,
    Reserved,
    Session,
    SlugPk,
    Subscriber,
    Tenant,
    Ticket,
    Ticketed,
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
    # Declaration order must not reach the generated SQL or the cache key.
    names = [name for name, _ in _order().columns()]

    assert names == sorted(names)
    assert names == ["channel", "created_at", "status", "total"]


def test_a_negative_row_count_is_refused() -> None:
    with pytest.raises(InvalidShape, match="cannot have -1 rows"):
        Table(Company, rows=-1, name=Constant("x"))


def test_zero_rows_is_allowed() -> None:
    # An empty table is what a parent with no children looks like, so it is allowed.
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
    # A value distribution over a foreign key emits keys pointing at rows that may not exist.
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
    # It is filled with exactly what save() would have written.
    channel = _order().fields["channel"]

    assert channel.value(0, 0.0) == "web"
    assert channel.value(999, 0.9) == "web"


def test_a_declared_distribution_wins_over_the_model_default() -> None:
    assert _order(channel=Constant("api")).fields["channel"].value(0, 0.0) == "api"


def test_a_callable_default_is_refused_rather_than_guessed() -> None:
    # uuid4 varies per row and dict does not, and this cannot tell them apart.
    with pytest.raises(InvalidShape, match="callable default"):
        Table(Defaulted, rows=1)


def test_a_field_colliding_with_the_signature_is_declarable_through_fields() -> None:
    # The escape hatch: Python binds rows to the row count before the field sees it.
    table = Table(Reserved, rows=3, fields={"rows": Sequential(0, 1)})

    assert [name for name, _ in table.columns()] == ["rows"]
    assert table.rows == 3


def test_a_key_type_with_no_obvious_strategy_is_refused_and_points_at_the_fix() -> None:
    # Refusing is only half of it.
    with pytest.raises(InvalidShape, match="CharField primary") as raised:
        Table(SlugPk, rows=3, name=Constant("x"))

    assert "keys=" in str(raised.value)
    assert "KeyFunction" in str(raised.value)


def test_an_integer_key_infers_a_counter() -> None:
    assert isinstance(_order().keys, SequentialKeys)


def test_a_uuid_key_infers_derived_uuids() -> None:
    # A UUID primary key was once refused outright, unusable for a whole class of projects.
    assert isinstance(Table(Tenant, rows=5, name=Constant("t")).keys, UuidKeys)


def test_an_explicit_strategy_makes_an_exotic_key_declarable() -> None:
    table = Table(SlugPk, rows=3, name=Constant("x"), keys=KeyFunction(lambda row: f"s-{row}"))

    assert table.keys.key_for(2, 0) == "s-2"


def test_an_explicit_strategy_overrides_the_inferred_one() -> None:
    table = Table(Company, rows=3, name=Constant("x"), keys=KeyFunction(lambda row: row * 10))

    assert table.keys.key_for(2, 0) == 20


def test_omitting_a_required_relation_is_refused_like_declaring_one() -> None:
    # Omitting a required relation reached COPY to die on a not-null violation.
    with pytest.raises(InvalidShape, match="Project.company cannot be null"):
        Table(Project, rows=1, status=Constant("ACTIVE"), created_at=Sequential(0, 1))


def test_a_forgotten_foreign_key_is_told_how_to_declare_one() -> None:
    # The commonest possible mistake, and so the first thing many readers ever see this package say.
    with pytest.raises(InvalidShape) as raised:
        Table(Project, rows=1, status=Constant("ACTIVE"), created_at=Sequential(0, 1))

    message = str(raised.value)
    assert "company=FanOut(Zipf())" in message
    assert "not supported" not in message
    assert "next release" not in message


def test_a_required_relation_carrying_a_python_default_is_refused_too() -> None:
    # A default= on a foreign key is applied by save(), which this package never calls.
    with pytest.raises(InvalidShape) as raised:
        Table(Assigned, rows=1, label=Constant("x"))

    message = str(raised.value)
    assert "Assigned.company cannot be null" in message
    assert "carries a default=, and a default is applied by save()" in message


def test_a_callable_default_on_a_required_relation_is_refused_by_the_same_check() -> None:
    # A callable default reading the database to find a parent slips two refusals at once.
    with pytest.raises(InvalidShape) as raised:
        Table(Delegated, rows=1, label=Constant("x"))

    message = str(raised.value)
    assert "Delegated.company cannot be null" in message
    assert "usually reads the database" in message


def test_a_relation_default_is_not_folded_into_a_constant() -> None:
    # A parent's keys come from the parent's table, so a default cannot stand in for a fan-out.
    with pytest.raises(InvalidShape) as raised:
        Table(Assigned, rows=1, label=Constant("x"))

    assert "company=FanOut(Zipf())" in str(raised.value)


def test_a_pairing_has_to_name_a_fan_out_this_table_declares() -> None:
    with pytest.raises(InvalidShape) as raised:
        Table(
            Membership,
            rows=10,
            company=FanOut(Zipf()),
            person=Paired("nowhere", Zipf()),
            role=Constant("r"),
        )

    message = str(raised.value)
    assert "paired with 'nowhere', which this table does not declare" in message
    # The correction is one read away rather than a second round trip.
    assert "Its fan-outs are: company" in message


def test_a_pairing_over_a_column_that_is_not_a_fan_out_is_refused() -> None:
    # A pairing takes its groups from a partition, and only a fan-out partitions the rows.
    with pytest.raises(InvalidShape, match="rather than a fan-out"):
        Table(
            Membership,
            rows=10,
            company=FanOut(Zipf()),
            person=Paired("role", Zipf()),
            role=Constant("r"),
        )


def test_a_pairing_with_itself_is_refused() -> None:
    with pytest.raises(InvalidShape, match="paired with itself"):
        Table(
            Membership,
            rows=10,
            company=FanOut(Zipf()),
            person=Paired("person", Zipf()),
            role=Constant("r"),
        )


def test_a_table_with_no_fan_out_at_all_says_that_rather_than_listing_none() -> None:
    with pytest.raises(InvalidShape) as raised:
        Table(Membership, rows=10, company=Paired("person", Zipf()), role=Constant("r"))

    assert "declares no fan-out at all" in str(raised.value)


def test_a_pairing_on_a_column_that_is_not_a_relation_is_refused() -> None:
    with pytest.raises(InvalidShape, match="has nothing to fan out over"):
        Table(
            Membership,
            rows=10,
            company=FanOut(Zipf()),
            person=Paired("company", Zipf()),
            role=Paired("company", Zipf()),
        )


def test_a_nullable_relation_may_be_omitted_and_loads_null() -> None:
    # Optional foreign keys are common, so they are allowed and load entirely NULL.
    assert "referrer" not in Table(Referred, rows=1, label=Constant("x")).fields


def test_a_draw_on_a_unique_column_is_refused_however_much_room_it_has() -> None:
    # A draw is not a permutation, and no amount of capacity makes it one.
    with pytest.raises(InvalidShape) as raised:
        Table(Subscriber, rows=5, email=Skew({f"a{i}@x.com": 1 for i in range(50)}))

    message = str(raised.value)
    assert "Subscriber.email is unique" in message
    assert "draws a value per row" in message
    assert "Sequential" in message


def test_a_unique_column_short_of_values_keeps_the_arithmetic_message() -> None:
    # The capacity check answers first, deliberately.
    with pytest.raises(InvalidShape) as raised:
        Table(Subscriber, rows=50, email=Skew({"a@x.com": 1, "b@x.com": 1}))

    assert "can only produce 2" in str(raised.value)


def test_a_distinct_declaration_keeps_a_unique_column_on_its_own() -> None:
    # Measured 20/20 where the draw was 10/20.
    Table(Subscriber, rows=50, email=Sequential("a", "b"))


def test_a_derivation_on_a_unique_column_is_left_alone() -> None:
    # Not an exemption but a different question.
    Table(
        Ticketed,
        rows=50,
        prefix=Sequential("a", "b"),
        reference=Derived("prefix", compute=lambda prefix: prefix),
    )


def test_a_fan_out_on_a_unique_column_is_left_to_the_shape() -> None:
    # Deciding this needs the parent's row count, which only a whole shape has.
    Table(
        Ticketed,
        rows=50,
        prefix=Constant("p"),
        reference=Sequential("r", "x"),
        company=FanOut(Zipf()),
    )


def test_a_single_row_cannot_collide_with_itself() -> None:
    # A table of one row has no second row to collide with, whatever fills the column.
    Table(Subscriber, rows=1, email=Constant("only@x.com"))


def test_a_constant_cannot_fill_a_unique_column_twice() -> None:
    # Arithmetic, decidable here.
    with pytest.raises(InvalidShape, match="needs 5 distinct values"):
        Table(Subscriber, rows=5, email=Constant("a@example.com"))


def test_a_skew_with_too_few_values_cannot_fill_a_unique_column() -> None:
    with pytest.raises(InvalidShape, match="can only produce 2"):
        Table(Subscriber, rows=5, email=Skew({"a@example.com": 1, "b@example.com": 1}))


def test_a_bounded_distribution_that_is_big_enough_is_accepted() -> None:
    assert Table(Subscriber, rows=1, email=Constant("a@example.com")).rows == 1


def test_an_unbounded_distribution_is_not_second_guessed() -> None:
    # A distribution that cannot answer the question should not be refused for failing to.
    assert not isinstance(Sequential(0, 1), Bounded)
    assert Table(Subscriber, rows=1000, email=Sequential(0, 1)).rows == 1000


def test_a_declaration_cannot_be_edited_past_its_own_validation() -> None:
    # Every rule in Table runs once, in __init__.
    table = _order()

    for attribute, value in (("rows", -1), ("model", Company), ("fields", {})):
        with pytest.raises(AttributeError):
            setattr(table, attribute, value)

    with pytest.raises(TypeError):
        table.fields["status"] = Constant("x")


@pytest.mark.skipif(django.VERSION < (5, 2), reason="composite primary keys arrived in Django 5.2")
def test_a_composite_primary_key_is_refused_as_arity_not_type() -> None:
    # The message has to say keys= cannot help, or the reader will try the escape hatch.
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

    # Within-row, across-the-parent twice, and a shared rank, all on one table.
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

    # Sorted by name, total precedes unit_price, so the two orders disagree.
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
    # Caught here rather than met as a None inside the arithmetic.
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
    # A derivation reading a model default is checked after Table fills it in.
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
    # The build adds parent columns to the query that already reads the keys, with no lookup.
    assert _ticket().parent_fields() == {"account": ("plan", "signed_up_at")}


def test_a_table_with_no_parent_derivations_reads_nothing_extra() -> None:
    assert Table(Session, rows=5, company=FanOut(Zipf()), label=Constant("s")).parent_fields() == {}


def test_a_statistics_target_on_a_field_the_model_does_not_have_is_refused() -> None:
    with pytest.raises(InvalidShape, match="Order has no field named nope") as raised:
        Table(
            Order,
            rows=3,
            status=Constant("a"),
            total=Constant(1),
            created_at=Sequential(0, 1),
            statistics={"nope": 200},
        )

    assert "statistics target" in str(raised.value)


def test_a_statistics_target_on_a_column_this_shape_leaves_empty_is_refused() -> None:
    # note is nullable and undeclared, so every row would hold the same nothing.
    with pytest.raises(InvalidShape, match="does not fill that column"):
        Table(
            Order,
            rows=3,
            status=Constant("a"),
            total=Constant(1),
            created_at=Sequential(0, 1),
            statistics={"note": 200},
        )


def test_a_statistics_target_on_a_column_the_model_defaulted_is_accepted() -> None:
    # Which is why the check runs after the defaults are resolved rather than before.
    table = Table(
        Order,
        rows=3,
        status=Constant("a"),
        total=Constant(1),
        created_at=Sequential(0, 1),
        statistics={"channel": 200},
    )

    assert table.statistics == {"channel": 200}


def test_a_statistics_target_on_the_primary_key_is_accepted() -> None:
    # The one column that is not in fields= and is still filled: this package assigns it.
    assert Table(Company, rows=3, name=Constant("acme"), statistics={"id": 400}).statistics == {
        "id": 400
    }


def test_the_targets_are_read_only_like_every_other_part_of_a_declaration() -> None:
    table = Table(Company, rows=3, name=Constant("acme"), statistics={"name": 400})

    with pytest.raises(TypeError):
        cast("Any", table.statistics)["name"] = 500


def test_a_table_that_asks_for_nothing_says_so_rather_than_guessing() -> None:
    # An empty mapping, because this package does not pick a target on the caller's behalf.
    assert Table(Company, rows=3, name=Constant("acme")).statistics == {}


_PROJECT_START = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)


def _project(**overrides: Any) -> Table:
    fields: dict[str, Any] = {
        "company": FanOut(Zipf(1.2), placement="grouped"),
        "created_at": Sequential(_PROJECT_START, datetime.timedelta(minutes=1)),
        "status": PerParent("company", last="ACTIVE", rest="COMPLETE"),
    }
    fields.update(overrides)
    return Table(Project, rows=20, fields=fields)


def test_a_group_scoped_column_is_accepted_over_a_declared_fan_out() -> None:
    table = _project()

    assert table.computation_order() == ("status",)


def test_a_group_with_no_fan_out_to_partition_it_is_refused() -> None:
    # A group source with no fan-out has no partition, so no per-group rule is computable.
    with pytest.raises(InvalidShape, match="not a fan-out declared on this table"):
        _project(status=PerParent("created_at", last="ACTIVE", rest="COMPLETE"))


def test_a_group_over_a_fan_out_with_a_null_share_is_refused() -> None:
    # Each NULL is its own group in a unique index, so those rows sit outside the rule.
    with pytest.raises(InvalidShape, match="belong to no group at all"):
        Table(
            Project,
            rows=20,
            company=FanOut(Zipf(), null=0.1),
            created_at=Sequential(_PROJECT_START, datetime.timedelta(minutes=1)),
            status=PerParent("company", last="ACTIVE", rest="COMPLETE"),
        )


def test_ordering_a_group_by_a_column_this_shape_does_not_fill_is_refused() -> None:
    with pytest.raises(InvalidShape, match="which this shape does not fill"):
        _project(
            status=PerParent("company", last="ACTIVE", rest="COMPLETE", order_by="finished_at")
        )


def test_ordering_a_group_by_a_column_that_does_not_climb_is_refused() -> None:
    with pytest.raises(InvalidShape, match="does not climb with the row index"):
        _project(
            created_at=Skew({_PROJECT_START: 1.0}),
            status=PerParent("company", last="ACTIVE", rest="COMPLETE", order_by="created_at"),
        )


def test_ordering_a_group_by_a_column_filled_backwards_is_refused() -> None:
    # Filled backwards, asking for each group's newest row would silently get the oldest.
    with pytest.raises(InvalidShape, match="does not climb with the row index"):
        _project(
            created_at=Sequential(_PROJECT_START, datetime.timedelta(minutes=-1)),
            status=PerParent("company", last="ACTIVE", rest="COMPLETE", order_by="created_at"),
        )


def test_ordering_a_group_under_arrival_placement_is_refused() -> None:
    # The incompatibility that is a meaning rather than a missing feature.
    with pytest.raises(InvalidShape, match="placement='arrival'") as raised:
        _project(
            company=FanOut(Zipf(1.2)),
            status=PerParent("company", last="ACTIVE", rest="COMPLETE", order_by="created_at"),
        )

    assert "placement='grouped'" in str(raised.value)
    assert "drop order_by" in str(raised.value)


def test_ordering_a_group_under_grouped_placement_is_accepted() -> None:
    table = _project(
        status=PerParent("company", last="ACTIVE", rest="COMPLETE", order_by="created_at")
    )

    assert table.computation_order() == ("status",)


def test_multi_table_inheritance_is_refused_and_says_so() -> None:
    # It had no working spelling at all.
    with pytest.raises(InvalidShape, match="multi-table inheritance") as raised:
        Table(
            DeliveryDocument,
            rows=5,
            title=Constant("t"),
            tracking=Constant("x"),
        )

    message = str(raised.value)
    assert "Document" in message
    assert "testapp_deliverydocument holds document_ptr_id, tracking" in message
    assert "testapp_document holds id, title" in message


def test_multi_table_inheritance_is_refused_before_anything_else_is_read() -> None:
    # Declaring nothing reaches the same refusal, before any complaint about a column.
    with pytest.raises(InvalidShape, match="multi-table inheritance"):
        Table(DeliveryDocument, rows=5)


def test_abstract_inheritance_is_not_the_refused_kind() -> None:
    # Abstract bases copy fields onto the child, so no column lands in another table.
    table = Table(Memo, rows=5, body=Constant("b"), created_at=Sequential(0, 1))

    assert [name for name, _ in table.columns()] == ["body", "created_at"]


def test_a_proxy_declares_the_table_it_proxies() -> None:
    # A proxy adds no column and no table, so a shape naming one is about the proxied table.
    table = Table(CompanyProxy, rows=5, name=Constant("acme"))

    assert table.db_table == Company._meta.db_table
