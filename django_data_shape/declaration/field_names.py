"""Columns by field name, however a uniqueness spelled them."""

from __future__ import annotations

from collections.abc import Iterable

from django.db.models import Model


def field_names(model: type[Model], columns: Iterable[str]) -> tuple[str, ...]:
    """These columns by field name. Django refuses a constraint naming anything else.

    ``UniqueConstraint(fields=)`` and ``unique_together`` both take ``parent`` and
    ``parent_id`` and keep them as written, and a shape is keyed on names, so text
    compared to ``field.name`` misses the attname. ``get_field`` answers to either.
    ``test_a_column_named_by_its_attname_is_the_column`` holds it for a refusal, and
    ``test_a_composite_unique_together_spelled_with_the_attname_is_a_finding`` for a report.
    """
    return tuple(model._meta.get_field(column).name for column in columns)
