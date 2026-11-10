from __future__ import annotations

import pytest
from django.db import connection, models
from django.test.utils import isolate_apps

from django_data_shape import Constant, FanOut, InvalidShape, Shape, Table, Zipf
from django_data_shape import build as build_shape
from tests.testapp.models import Company

with isolate_apps("tests.testapp"):

    class UniqueOnTheField(models.Model):
        """The company column unique by ``unique=True``, which writes no constraint."""

        company = models.OneToOneField(Company, null=True, on_delete=models.CASCADE)

        class Meta:
            app_label = "testapp"

    class UniqueByAConstraint(models.Model):
        """The same index, written as a ``UniqueConstraint`` over the one column."""

        company = models.ForeignKey(Company, null=True, on_delete=models.CASCADE)

        class Meta:
            app_label = "testapp"
            constraints = [
                models.UniqueConstraint(fields=["company"], name="one_row_per_company"),
            ]


_SPELLINGS = pytest.mark.parametrize(
    "model", [UniqueOnTheField, UniqueByAConstraint], ids=["unique_true", "unique_constraint"]
)


def _companies() -> Table:
    return Table(Company, rows=50, name=Constant("acme"))


@_SPELLINGS
def test_one_row_under_a_fan_out_alone_is_accepted_either_way(model: type[models.Model]) -> None:
    # One row has no second to collide with, so a fan-out alone is accepted either way.
    Shape(_companies(), Table(model, rows=1, company=FanOut(Zipf())))


@_SPELLINGS
def test_two_rows_under_a_fan_out_alone_are_refused_either_way(model: type[models.Model]) -> None:
    with pytest.raises(InvalidShape) as raised:
        Shape(_companies(), Table(model, rows=2, company=FanOut(Zipf())))

    message = str(raised.value)
    assert f"{model.__name__}.company" in message
    assert "max_children=1, which caps every parent at one row" in message


@pytest.mark.skipif(connection.vendor != "postgresql", reason="it loads into a unique index")
@pytest.mark.django_db(transaction=True)
@_SPELLINGS
def test_one_row_under_a_fan_out_alone_loads_either_way(model: type[models.Model]) -> None:
    with connection.schema_editor() as editor:
        editor.create_model(model)
    try:
        for seed in (0, 1, 7, 42, 1000):
            build_shape(
                Shape(_companies(), Table(model, rows=1, company=FanOut(Zipf())), seed=seed)
            )
            assert model._default_manager.count() == 1
            with connection.cursor() as cursor:
                cursor.execute(
                    f"TRUNCATE {connection.ops.quote_name(model._meta.db_table)}, "
                    f"{connection.ops.quote_name(Company._meta.db_table)} CASCADE"
                )
    finally:
        with connection.schema_editor() as editor:
            editor.delete_model(model)
