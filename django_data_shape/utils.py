"""Helpers used across more than one module."""

from __future__ import annotations

import hashlib
from typing import Any

from django.db.models import Field, Model
from django.db.models.fields import NOT_PROVIDED

from django_data_shape.invalid_shape import InvalidShape

_MASK64 = (1 << 64) - 1
_GOLDEN = 0x9E3779B97F4A7C15


def primary_key_field(model: type[Model]) -> Field[Any, Any]:
    """The one concrete field that is ``model``'s primary key, or a refusal.

    A composite primary key has no column of its own, so it is not among the
    concrete fields at all and the obvious lookup raises a bare
    ``StopIteration`` from inside this package -- which says nothing about what
    the caller did. The message has to say ``keys=`` cannot help either, or a
    reader told only "unsupported" will reasonably try the escape hatch that
    solves every other unusual key and fail twice.

    Shared rather than written at each entry point because both routes into a
    table -- generating rows for it, and projecting into it -- ask this same
    question first, and a refusal worded two ways is a refusal that will drift.
    """
    field = next((field for field in model._meta.concrete_fields if field.primary_key), None)
    if field is None:
        raise InvalidShape(
            f"{model.__name__} has a composite primary key, which this package cannot assign. A "
            "key strategy maps a row index to one value and a composite key is several columns, "
            "so keys= cannot help either: this is arity, not type."
        )
    return field


def has_db_default(field: Field[Any, Any]) -> bool:
    """Whether the database itself will supply a value for this column.

    ``db_default`` arrived in Django 5.0 and this package supports 4.2, so the
    attribute cannot be assumed to exist. Unlike ``default``, this one is real
    DDL, which is why a column carrying it can be left out of a ``COPY`` or an
    ``INSERT ... SELECT`` entirely and still be filled.

    Here rather than beside either caller because both routes into a table ask
    the same question of a column, and the Django-version detail behind the
    answer is one nobody should have to find twice.
    """
    return getattr(field, "db_default", NOT_PROVIDED) is not NOT_PROVIDED


def field_stream(seed: int, table: str, field: str) -> int:
    """A stable 64-bit stream id for one table's one field.

    Derived once per field rather than per row, so the cost of a real hash is
    paid a handful of times instead of millions. ``hash()`` is deliberately not
    used: it is salted per interpreter run, which would make a seeded shape
    reproduce only within a single process.
    """
    digest = hashlib.blake2b(f"{table}.{field}".encode(), digest_size=8).digest()
    return (int.from_bytes(digest, "big") ^ seed) & _MASK64


def draw(stream: int, row: int) -> float:
    """A uniform float in [0, 1) for one field of one row.

    Deterministic in ``(stream, row)`` alone, which is the property the whole
    design rests on: a value does not depend on how many rows were generated
    before it, so rows can later be emitted in an order different from the one
    they were assigned in. That separation is what lets physical placement be
    declared without buffering a group in memory.

    SplitMix64's finalizer rather than ``random.Random``: seeding a Mersenne
    Twister per value costs far more than the value is worth at these row
    counts, and this needs no sequential state to be reproducible.
    """
    z = (stream + (row + 1) * _GOLDEN) & _MASK64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
    z = z ^ (z >> 31)
    # 53 bits over 2**53 rather than 64 bits over 2**64: the latter can round up
    # to exactly 1.0 for the top 1024 finalizer outputs, and the finalizer is a
    # bijection so all of them are reachable. That would break the [0, 1)
    # contract every distribution is written against. This is the same
    # construction the standard library uses for ``random.random()``.
    return (z >> 11) / 9007199254740992.0
