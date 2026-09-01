"""Every refusal Table makes, and why it makes it."""

from __future__ import annotations

from typing import Any, cast

import pytest

from django_data_shape import (
    Bounded,
    Constant,
    Distribution,
    InvalidShape,
    Sequential,
    Skew,
    Table,
    Uniform,
)
from tests.testapp.models import (
    Company,
    Defaulted,
    Order,
    Project,
    Referred,
    Reserved,
    SlugPk,
    Subscriber,
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


def test_declaring_a_relation_is_refused_for_now() -> None:
    with pytest.raises(InvalidShape, match="is a relation"):
        Table(
            Project,
            rows=1,
            company=Constant(1),
            status=Constant("ACTIVE"),
            created_at=Sequential(0, 1),
        )


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


def test_a_non_integer_primary_key_is_refused() -> None:
    # It used to load: the dense 1..N range went into the CharField verbatim and
    # wrote "1", "2", "3" -- values the application could never produce, with a
    # whole statistics picture built on top of them, and no error anywhere.
    with pytest.raises(InvalidShape, match="CharField primary key"):
        Table(SlugPk, rows=3, name=Constant("x"))


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


def test_a_database_default_is_left_to_the_database() -> None:
    # A stub rather than a model, so this runs on Django 4.2 as well -- db_default
    # arrived in 5.0, and the branch it guards is the only reason a column may
    # legitimately be left out of the COPY. Mutating _has_db_default to return
    # False left the whole suite green before this existed, because the `or`
    # arm next to it was already covered.
    class _Field:
        db_default = "eu"

    class _Older:
        pass

    assert Table._has_db_default(cast("Any", _Field()))
    assert not Table._has_db_default(cast("Any", _Older()))


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
