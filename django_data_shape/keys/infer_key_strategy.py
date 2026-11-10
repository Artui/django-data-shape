from __future__ import annotations

from typing import Any

from django.db.models import Field, IntegerField, UUIDField

from django_data_shape.keys.key_strategy import KeyStrategy
from django_data_shape.keys.sequential_keys import SequentialKeys
from django_data_shape.keys.uuid_keys import UuidKeys


def infer_key_strategy(field: Field[Any, Any]) -> KeyStrategy | None:
    """The obvious strategy for a primary key type, or None if there is none."""
    if isinstance(field, IntegerField):
        return SequentialKeys()
    if isinstance(field, UUIDField):
        return UuidKeys()
    return None
