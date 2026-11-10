from __future__ import annotations

from collections.abc import Iterable

from django.db.models import Model


def field_names(model: type[Model], columns: Iterable[str]) -> tuple[str, ...]:
    """These columns by field name. Django refuses a constraint naming anything else."""
    return tuple(model._meta.get_field(column).name for column in columns)
