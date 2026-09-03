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
    DeliveryDocument,
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
    with pytest.raises(InvalidShape, match="Project.company cannot be null"):
        Table(Project, rows=1, status=Constant("ACTIVE"), created_at=Sequential(0, 1))


def test_a_forgotten_foreign_key_is_told_how_to_declare_one() -> None:
    # The commonest possible mistake, and so the first thing many readers ever
    # see this package say. It used to say relations were unsupported and that
    # fan-out was coming in the next release -- true when it was written, wrong
    # from the release after, and read by a first-time user as "this package
    # cannot do the thing it is for". The remedy has to be in the message, and
    # named after the field that is missing rather than in the abstract.
    with pytest.raises(InvalidShape) as raised:
        Table(Project, rows=1, status=Constant("ACTIVE"), created_at=Sequential(0, 1))

    message = str(raised.value)
    assert "company=FanOut(Zipf())" in message
    assert "not supported" not in message
    assert "next release" not in message


def test_a_required_relation_carrying_a_python_default_is_refused_too() -> None:
    # The hole the first version of this refusal left open, and it fails exactly
    # the way the refusal exists to prevent. A `default=` on a foreign key is
    # applied by save(), which this package never calls, so the column was
    # neither refused (the check skipped anything with a default) nor filled
    # (the fill loop skipped anything that is a relation) -- and the load died
    # inside COPY on "null value in column company_id".
    #
    # Reported by a consumer against the release before the refusal existed, so
    # their case is fixed; this narrower one survived it.
    with pytest.raises(InvalidShape, match="Assigned.company cannot be null"):
        Table(Assigned, rows=1, label=Constant("x"))


def test_a_relation_default_is_not_folded_into_a_constant() -> None:
    # The reading that would have been wrong: filling company_id with the
    # default's value is what _resolve_defaults does for a scalar, and doing it
    # here would emit a key drawn from nothing -- the very thing declaring a
    # value distribution on a relation is refused for. A parent's keys come from
    # the parent's table, so a default cannot stand in for a fan-out.
    with pytest.raises(InvalidShape) as raised:
        Table(Assigned, rows=1, label=Constant("x"))

    assert "company=FanOut(Zipf())" in str(raised.value)


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
    # A bigger sample of a column of NULLs describes nothing more precisely, so
    # the target is a promise that could not be kept.
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
    # channel is never declared -- Table fills it from the model's own default --
    # and it is a column this shape writes, so it can carry a target. Which is
    # why the check runs after the defaults are resolved rather than before.
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
    # The one column that is not in fields= and is still filled: this package
    # assigns it. Refusing it would be refusing a target on the column every
    # foreign key in the database points at.
    assert Table(Company, rows=3, name=Constant("acme"), statistics={"id": 400}).statistics == {
        "id": 400
    }


def test_the_targets_are_read_only_like_every_other_part_of_a_declaration() -> None:
    table = Table(Company, rows=3, name=Constant("acme"), statistics={"name": 400})

    with pytest.raises(TypeError):
        cast("Any", table.statistics)["name"] = 500


def test_a_table_that_asks_for_nothing_says_so_rather_than_guessing() -> None:
    # An empty mapping rather than a default filled in from somewhere: a column
    # left out keeps whatever target the schema gives it, and this package does
    # not decide that on the caller's behalf.
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
    # A parent source with no fan-out has nothing to read; a group source with
    # no fan-out has no partition, and it is the partition that makes a
    # per-group rule computable at all.
    with pytest.raises(InvalidShape, match="not a fan-out declared on this table"):
        _project(status=PerParent("created_at", last="ACTIVE", rest="COMPLETE"))


def test_a_group_over_a_fan_out_with_a_null_share_is_refused() -> None:
    # PostgreSQL counts each NULL as its own group in a unique index, so those
    # rows would sit outside the very rule the declaration exists to keep.
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
    # Sequential implements the protocol and still answers no, which is the
    # reason the protocol is a question rather than a marker: a declaration
    # asking for the newest row of each group while filling the column
    # backwards would silently get the oldest.
    with pytest.raises(InvalidShape, match="does not climb with the row index"):
        _project(
            created_at=Sequential(_PROJECT_START, datetime.timedelta(minutes=-1)),
            status=PerParent("company", last="ACTIVE", rest="COMPLETE", order_by="created_at"),
        )


def test_ordering_a_group_under_arrival_placement_is_refused() -> None:
    # The incompatibility that is a meaning rather than a missing feature.
    # Arrival interleaves a parent's children through the table on purpose, so
    # their row indices are scattered and the last row of a group is not the
    # greatest created_at. The message has to name both ways out.
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
    # It had no working spelling at all. Declaring the parent's columns was
    # accepted -- _meta.concrete_fields spans both tables -- and then the
    # statistics pass read the child table's own catalogue and raised a bare
    # KeyError from inside the loader, naming neither the model, the column nor
    # inheritance. Omitting them instead hit the missing-column refusal, which
    # is a true sentence about a column that is not the child's to fill.
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
    # Declaring nothing at all reaches the same refusal rather than a complaint
    # about title, which is a column this declaration could never have filled
    # from here. A reader told to declare it would declare it and be no better
    # off, which is the shape of an error that knows less than it says.
    with pytest.raises(InvalidShape, match="multi-table inheritance"):
        Table(DeliveryDocument, rows=5)


def test_abstract_inheritance_is_not_the_refused_kind() -> None:
    # By far the commoner inheritance, and a refusal that caught it would be a
    # serious regression: Django copies an abstract base's fields onto the
    # child, so every column belongs to the child's own table and nothing lands
    # next door. The two kinds are told apart by which table a field's own model
    # writes to, which is the only test that separates them.
    table = Table(Memo, rows=5, body=Constant("b"), created_at=Sequential(0, 1))

    assert [name for name, _ in table.columns()] == ["body", "created_at"]


def test_a_proxy_declares_the_table_it_proxies() -> None:
    # Not the inherited case, and it must not be caught by it: a proxy adds no
    # column and no table, so a shape naming one is a shape about the table it
    # proxies. _meta.parents is populated for a proxy too, which is why the test
    # is which table a field's own model writes to.
    table = Table(CompanyProxy, rows=5, name=Constant("acme"))

    assert table.db_table == Company._meta.db_table
