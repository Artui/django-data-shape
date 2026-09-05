"""The draw derivation everything else rests on, and the two shared lookups."""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.db import models

from django_data_shape import InvalidShape
from django_data_shape.utils import (
    MAX_STATISTICS_TARGET,
    check_statistics_target,
    draw,
    field_stream,
    has_db_default,
    offsettable_kind,
    primary_key_field,
)
from tests.testapp.models import Company


def test_a_draw_is_uniform_in_the_unit_interval() -> None:
    stream = field_stream(seed=7, table="orders", field="status")
    values = [draw(stream, row) for row in range(2000)]

    assert all(0.0 <= value < 1.0 for value in values)
    # The upper bound is a guarantee, not an observation: the largest value the
    # derivation can produce is below 1.0 by construction, which is what Skew's
    # bounds and the Distribution contract are written against.
    assert (((1 << 64) - 1) >> 11) / 9007199254740992.0 < 1.0
    # Not a distribution test, a smoke test: a mixer that collapsed would show
    # up here long before it showed up as a wrong-looking database.
    assert len(set(values)) > 1900


def test_a_draw_depends_only_on_its_stream_and_row() -> None:
    stream = field_stream(seed=7, table="orders", field="status")

    # The property the placement work depends on: asking for row 900 first does
    # not change what row 900 is. A generator carrying sequential RNG state
    # would fail this, and would then be unable to emit rows out of order.
    forwards = [draw(stream, row) for row in range(1000)]
    backwards = [draw(stream, row) for row in reversed(range(1000))]

    assert forwards == list(reversed(backwards))


def test_fields_and_seeds_produce_independent_streams() -> None:
    status = field_stream(seed=7, table="orders", field="status")
    total = field_stream(seed=7, table="orders", field="total")
    other_table = field_stream(seed=7, table="projects", field="status")
    other_seed = field_stream(seed=8, table="orders", field="status")

    assert len({status, total, other_table, other_seed}) == 4
    assert draw(status, 0) != draw(total, 0)


def test_a_stream_is_stable_across_processes() -> None:
    # Pinned, not recomputed. ``hash()`` is salted per interpreter run, so a
    # shape seeded today would reproduce only within one process if the stream
    # derivation ever regressed to it -- and the failure would look like flaky
    # test data rather than like a bug here.
    assert field_stream(seed=0, table="orders", field="status") == 10965613546237361956
    assert draw(field_stream(seed=0, table="orders", field="status"), 0) == 0.9705692634847262


def test_a_database_default_is_left_to_the_database() -> None:
    # A stub rather than a model, so this runs on Django 4.2 as well -- db_default
    # arrived in 5.0, and the branch it guards is the only reason a column may
    # legitimately be left out of a COPY or an INSERT ... SELECT. Mutating this
    # helper to return False left the whole suite green before this existed,
    # because the `or` arm next to its one caller was already covered.
    class _Field:
        db_default = "eu"

    class _Older:
        pass

    assert has_db_default(cast("Any", _Field()))
    assert not has_db_default(cast("Any", _Older()))


def test_the_primary_key_lookup_finds_the_one_concrete_key_field() -> None:
    # Shared by both routes into a table, so it is tested where it lives rather
    # than twice at the entry points. The refusal it makes for a composite key
    # is version-gated and lives beside the declarations that reach it.
    assert primary_key_field(Company).name == "id"


def test_a_model_with_no_concrete_primary_key_is_refused_as_arity() -> None:
    class _Meta:
        concrete_fields: tuple[object, ...] = ()

    class _Keyless:
        __name__ = "Keyless"
        _meta = _Meta()

    with pytest.raises(InvalidShape, match="arity, not type"):
        primary_key_field(cast("Any", _Keyless()))


@pytest.mark.parametrize("target", [1, 100, MAX_STATISTICS_TARGET])
def test_a_statistics_target_inside_postgres_own_range_is_accepted(target: int) -> None:
    check_statistics_target("Order.status", target)


def test_a_target_of_zero_is_refused_with_what_it_would_have_meant() -> None:
    # PostgreSQL accepts it, and it means "collect no statistics for this
    # column" -- which is the state this package exists to condemn rather than a
    # way of saying the column does not matter. So it is told, not obeyed.
    with pytest.raises(InvalidShape, match="collect no statistics") as raised:
        check_statistics_target("Order.status", 0)

    assert "Order.status" in str(raised.value)


def test_a_negative_target_is_refused_for_the_same_reason() -> None:
    # -1 is PostgreSQL's own spelling of "use the default", and it is refused
    # rather than passed through: a declaration that wants the default says so
    # by leaving the column out, and one that names a number should get that
    # number.
    with pytest.raises(InvalidShape, match="below one"):
        check_statistics_target("Order.status", -1)


def test_a_target_above_the_ceiling_is_refused_before_the_load_rather_than_after() -> None:
    with pytest.raises(InvalidShape, match="ceiling is 10000"):
        check_statistics_target("Order.status", MAX_STATISTICS_TARGET + 1)


@pytest.mark.parametrize("target", [1.5, "200", True])
def test_a_target_that_is_not_a_whole_number_is_refused(target: object) -> None:
    # True included, and deliberately: it is an int as far as Python is
    # concerned, so a bare isinstance check would accept it and ask PostgreSQL
    # for a statistics target of one.
    with pytest.raises(InvalidShape, match="not a whole number"):
        check_statistics_target("Order.status", cast("int", target))


# ---------------------------------------------------------------------------
# offsettable_kind: what a column holds, for the purpose of adding to it.
# Tested directly as well as through `After`, because the ordering inside it
# is load-bearing and invisible from outside -- `DateTimeField` subclasses
# `DateField`, so a chain asking the questions the other way round would call
# every timestamp a date and refuse nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        (models.DateTimeField(), "datetime"),
        (models.DateField(), "date"),
        (models.TimeField(), "time"),
        (models.DurationField(), "duration"),
        (models.IntegerField(), "number"),
        (models.BigIntegerField(), "number"),
        (models.FloatField(), "number"),
        (models.DecimalField(max_digits=5, decimal_places=2), "number"),
    ],
)
def test_each_kind_is_named(field: models.Field, expected: str) -> None:
    assert offsettable_kind(field) == expected


def test_a_datetime_is_not_read_as_a_date() -> None:
    """The one ordering that matters: DateTimeField *is* a DateField."""
    assert isinstance(models.DateTimeField(), models.DateField)

    assert offsettable_kind(models.DateTimeField()) != offsettable_kind(models.DateField())


@pytest.mark.parametrize(
    "field",
    [models.CharField(max_length=1), models.JSONField(), models.BooleanField()],
)
def test_a_column_with_no_opinion_declines_to_judge(field: models.Field) -> None:
    """None rather than a guess: the caller then refuses nothing, which is the
    safe direction. A custom field that adds cleanly to an offset is a
    legitimate declaration, and refusing it would cost a working shape."""
    assert offsettable_kind(field) is None
