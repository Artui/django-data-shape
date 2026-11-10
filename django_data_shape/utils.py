from __future__ import annotations

import hashlib
import numbers
import re
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from typing import Any, cast

from django.core.exceptions import ValidationError
from django.core.management.color import no_style
from django.db.models import (
    DateField,
    DateTimeField,
    DecimalField,
    DurationField,
    Field,
    FloatField,
    IntegerField,
    Model,
    TimeField,
)
from django.db.models.fields import NOT_PROVIDED

from django_data_shape.declaration.invalid_shape import InvalidShape

_MASK64 = (1 << 64) - 1
_GOLDEN = 0x9E3779B97F4A7C15

# Every template this package makes starts with this, so a machine's caches can
# be listed and dropped as a group. The rest of the name is a digest and nothing
# else -- a readable name would have to come from somewhere, and the only
# candidate is the declaration, which is exactly what the digest already is.
TEMPLATE_PREFIX = "data_shape_"

# The name a template is built under, and renamed away from once it is complete.
# Existence of the final name is therefore the same claim as "this is finished",
# which is what lets a cache check be one row from ``pg_database`` rather than a
# marker inside a database nothing is allowed to connect to.
PARTIAL_SUFFIX = "__partial"

# 8 bytes: sixteen hexadecimal characters, so the longest name a template gets --
# prefix, digest and the partial suffix -- is 36 of PostgreSQL's 63.
TEMPLATE_KEY_BYTES = 8

# A finished template this package made, built and derived alike: the prefix and
# the key's hexadecimal digest at its fixed length. The prefix alone would also
# match a user's own ``data_shape_userdb``.
FINISHED_TEMPLATE = re.compile(rf"{re.escape(TEMPLATE_PREFIX)}[0-9a-f]{{{TEMPLATE_KEY_BYTES * 2}}}")


def primary_key_field(model: type[Model]) -> Field[Any, Any]:
    """The one concrete field that is ``model``'s primary key, or a refusal."""
    field = next((field for field in model._meta.concrete_fields if field.primary_key), None)
    if field is None:
        raise InvalidShape(
            f"{model.__name__} has a composite primary key, which this package cannot assign. A "
            "key strategy maps a row index to one value and a composite key is several columns, "
            "so keys= cannot help either: this is arity, not type."
        )
    return field


def named_keys(
    named: tuple[object, ...] | None, model: type[Model], table: str, field: str
) -> tuple[object, ...] | None:
    """The keys ``parents=`` named, each read as ``model``'s primary key reads it."""
    if named is None:
        return None
    key_field = model._meta.pk
    keys: dict[object, object] = {}
    for spelling in named:
        try:
            key = key_field.to_python(spelling)
        except (ValidationError, ValueError, TypeError):
            raise InvalidShape(
                f"{table}.{field} names {spelling!r} in parents=, which cannot be a key of "
                f"{model.__name__}: its primary key {key_field.name} is a "
                f"{type(key_field).__name__}. Name the row by its primary key."
            ) from None
        if isinstance(spelling, bool) or (
            isinstance(key, numbers.Integral)
            and not isinstance(spelling, str)
            and not (isinstance(spelling, numbers.Number) and key == spelling)
        ):
            raise InvalidShape(
                f"{table}.{field} names {spelling!r} in parents=, which {model.__name__}'s "
                f"primary key reads as {key!r}: a row the declaration does not name. Name the "
                "row by its primary key, as an integer or its string."
            )
        if key in keys:
            if keys[key] == spelling:
                raise InvalidShape(
                    f"{table}.{field} names {model.__name__} {key!r} twice in parents=. A "
                    "repeated key weighs that row twice, which is a different partition from "
                    "the one this declaration reads as."
                )
            raise InvalidShape(
                f"{table}.{field} names {model.__name__} {key!r} more than once in parents=, as "
                f"{keys[key]!r} and as {spelling!r}. Both spell the same row, and a repeated key "
                "weighs that row twice, which is a different partition from the one this "
                "declaration reads as."
            )
        keys[key] = spelling
    return tuple(keys)


def has_db_default(field: Field[Any, Any]) -> bool:
    """Whether the database itself will supply a value for this column."""
    return getattr(field, "db_default", NOT_PROVIDED) is not NOT_PROVIDED


# PostgreSQL's own ceiling: ``ALTER TABLE ... SET STATISTICS`` rejects anything
# above this with "statistics target %d is too high", and refusing here means the
# reader is told at declaration time rather than partway through a build.
MAX_STATISTICS_TARGET = 10_000


def check_statistics_target(where: str, target: int) -> None:
    """Refuse a statistics target the planner could not act on."""
    if not isinstance(target, int) or isinstance(target, bool):
        raise InvalidShape(
            f"{where} has a statistics target of {target!r}, which is not a whole number. A "
            "target is a count of buckets: the planner keeps at most that many most-common "
            "values and that many histogram bounds, and samples 300 times as many rows."
        )
    if target < 1:
        raise InvalidShape(
            f"{where} has a statistics target of {target}, and PostgreSQL reads anything below "
            "one as 'collect no statistics for this column'. That is the state this package "
            "exists to condemn -- rows the planner cannot see are worse than no rows, because it "
            "guesses a default selectivity and commits to it. Leave the column out of "
            "statistics= to keep the schema's own target."
        )
    if target > MAX_STATISTICS_TARGET:
        raise InvalidShape(
            f"{where} has a statistics target of {target}, and PostgreSQL's ceiling is "
            f"{MAX_STATISTICS_TARGET}. ALTER TABLE would refuse it, after the load."
        )


def field_stream(seed: int, table: str, field: str) -> int:
    """A stable 64-bit stream id for one table's one field."""
    digest = hashlib.blake2b(f"{table}.{field}".encode(), digest_size=8).digest()
    return (int.from_bytes(digest, "big") ^ seed) & _MASK64


def draw(stream: int, row: int) -> float:
    """A uniform float in [0, 1) for one field of one row."""
    z = (stream + (row + 1) * _GOLDEN) & _MASK64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
    z = z ^ (z >> 31)
    return (z >> 11) / 9007199254740992.0


def many_to_many_refusal(model: type[Model], names: Iterable[str]) -> str | None:
    """The refusal for the first of ``names`` that is a many-to-many field, or ``None``."""
    fields = {field.name: field for field in model._meta.many_to_many}
    for name in names:
        if name in fields:
            through = cast(type[Model], fields[name].remote_field.through).__name__
            return (
                f"{model.__name__}.{name} is a many-to-many field, which is not a column of "
                f"{model.__name__}: its rows are the through model's. Declare the through model "
                f"{through} as a table of its own, with FanOut for one end and Paired for the "
                "other (see Relations, under Many-to-many), and leave "
                f"{name} out of this declaration."
            )
    return None


def check_not_inherited(model: type[Model]) -> None:
    """Refuse a model whose rows would land in more than one table."""
    db_table = str(model._meta.db_table)
    elsewhere: dict[type[Model], list[str]] = {}
    here: list[str] = []
    for field in model._meta.concrete_fields:
        owner = field.model
        if str(owner._meta.db_table) == db_table:
            here.append(str(field.column))
        else:
            elsewhere.setdefault(owner, []).append(str(field.column))
    if not elsewhere:
        return
    parents = ", ".join(parent.__name__ for parent in elsewhere)
    split = "; ".join(
        f"{parent._meta.db_table} holds {', '.join(sorted(columns))}"
        for parent, columns in elsewhere.items()
    )
    raise InvalidShape(
        f"{model.__name__} inherits from {parents} through multi-table inheritance, so one of "
        f"its rows is two rows: {db_table} holds {', '.join(sorted(here))}, and {split}. This "
        "package fills one table per declaration and assigns that table's keys itself as a "
        "dense 1..N range, so it can write either half and has nothing to pair them with -- and "
        "declaring the two separately does not work either, because the child's primary key is "
        "a foreign key to the parent and a fan-out is a partition rather than a bijection. The "
        "disagreement is in the declaration too: _meta.concrete_fields spans both tables while "
        f"db_table names one, so naming an inherited column is accepted and then loads {db_table} "
        f"with a column it does not have. Multi-table inheritance is not supported. Declare "
        f"{parents} on its own for the columns that live in its table, and make the rows of "
        f"{model.__name__} another way."
    )


def offsettable_kind(field: Field[Any, Any]) -> str | None:
    """What kind of thing this column holds, for the purpose of adding to it."""
    if isinstance(field, DateTimeField):
        return "datetime"
    if isinstance(field, DateField):
        return "date"
    if isinstance(field, TimeField):
        return "time"
    if isinstance(field, DurationField):
        return "duration"
    if isinstance(field, (IntegerField, FloatField, DecimalField)):
        return "number"
    return None


def reset_sequence(connection: Any, model: type[Model]) -> None:
    """Point the identity sequence at the rows the table currently holds."""
    with connection.cursor() as cursor:
        for statement in connection.ops.sequence_reset_sql(no_style(), [model]):
            cursor.execute(statement)


def absorb(hasher: hashlib.blake2b, part: str) -> None:
    """One string, length-prefixed so two parts cannot run together into a third."""
    encoded = part.encode()
    hasher.update(len(encoded).to_bytes(8, "big"))
    hasher.update(encoded)


def template_lock_id(name: str) -> int:
    """A signed 64-bit advisory lock id, which is the only shape PostgreSQL takes."""
    return int.from_bytes(
        hashlib.blake2b(name.encode(), digest_size=8).digest(), "big", signed=True
    )


@contextmanager
def pointed_at(connection: Any, database: str) -> Generator[None]:
    """Point an existing connection at another database for the length of the block."""
    original = connection.settings_dict["NAME"]
    connection.close()
    connection.settings_dict["NAME"] = database
    try:
        yield
    finally:
        connection.close()
        connection.settings_dict["NAME"] = original
