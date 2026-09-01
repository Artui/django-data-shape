"""Choosing a key strategy from the model, when the caller did not."""

from __future__ import annotations

from typing import Any

from django.db.models import Field, IntegerField, UUIDField

from django_data_shape.keys.key_strategy import KeyStrategy
from django_data_shape.keys.sequential_keys import SequentialKeys
from django_data_shape.keys.uuid_keys import UuidKeys


def infer_key_strategy(field: Field[Any, Any]) -> KeyStrategy | None:
    """The obvious strategy for a primary key type, or None if there is none.

    Only the two types where the right answer is unambiguous. Everything else
    returns None and is refused unless the caller declares a strategy, because
    inventing values for a semantic column is how a character primary key once
    got loaded with the strings "1", "2" and "3" -- data the application could
    never have written, with a whole statistics picture built on top of it.
    """
    if isinstance(field, IntegerField):
        return SequentialKeys()
    if isinstance(field, UUIDField):
        return UuidKeys()
    return None
