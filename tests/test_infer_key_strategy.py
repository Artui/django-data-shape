"""Which strategy a primary key type implies, when the caller says nothing."""

from __future__ import annotations

from django.db import models

from django_data_shape import SequentialKeys, UuidKeys
from django_data_shape.infer_key_strategy import infer_key_strategy


def test_an_integer_key_counts() -> None:
    assert isinstance(infer_key_strategy(models.BigAutoField()), SequentialKeys)
    assert isinstance(infer_key_strategy(models.IntegerField()), SequentialKeys)


def test_a_uuid_key_is_derived() -> None:
    assert isinstance(infer_key_strategy(models.UUIDField()), UuidKeys)


def test_anything_else_has_no_obvious_answer() -> None:
    # None rather than a guess. Inventing values for a semantic column is how a
    # character primary key once got loaded with "1", "2" and "3".
    assert infer_key_strategy(models.CharField()) is None
    assert infer_key_strategy(models.DateField()) is None
