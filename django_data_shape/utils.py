"""Helpers used across more than one module."""

from __future__ import annotations

import hashlib
from typing import Any

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


# PostgreSQL's own ceiling: ``ALTER TABLE ... SET STATISTICS`` rejects anything
# above this with "statistics target %d is too high", and refusing here means the
# reader is told at declaration time rather than partway through a build.
MAX_STATISTICS_TARGET = 10_000


def check_statistics_target(where: str, target: int) -> None:
    """Refuse a statistics target the planner could not act on.

    Shared because a table and a projection both accept one and a refusal
    worded two ways is a refusal that will drift.

    Zero is refused rather than passed through, and it is the interesting end.
    PostgreSQL accepts it, and it means *collect no statistics for this column*
    -- which is precisely the state this package exists to condemn. A column
    with rows and no statistics is worse than a column with no rows, because the
    planner falls back to a default selectivity and commits to it. A declaration
    that wanted to say "this column does not matter" has said something much
    stronger by accident, so it is told rather than obeyed.
    """
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


def check_not_inherited(model: type[Model]) -> None:
    """Refuse a model whose rows would land in more than one table.

    Multi-table inheritance gives the child a table of its own holding only the
    columns it declared, and leaves the rest next door in the parent's. One
    logical row is then two physical rows sharing a key, and this package fills
    one table per declaration and assigns that table's keys itself as a dense
    1..N range -- so it can write either half and has nothing to pair them with.
    Building the two as separate declarations does not work either: the child's
    primary key is a foreign key to the parent, and a fan-out is a partition
    rather than the bijection that would need to be.

    **The declaration itself is where the disagreement shows**, which is why
    this is a refusal rather than a gap. ``_meta.concrete_fields`` spans both
    tables while ``db_table`` names one, so naming an inherited column is
    accepted here and then loads a column the table does not have. That used to
    surface as a bare ``KeyError`` from inside the statistics pass, naming
    neither the model, the column nor inheritance.

    A proxy is not this case and must not be caught by it: it declares no column
    and no table of its own, so a shape naming a proxy is a shape about the
    table it proxies. The test is therefore which table a field's own model
    writes to rather than whether ``_meta.parents`` is populated -- that is true
    for a proxy too, and it is false for a proxy of an inheriting model, which
    is exactly the case a parents check would wave through.

    Shared because both routes into a table -- generating rows for it, and
    projecting into it -- write columns read off ``_meta.concrete_fields``, so
    both are wrong in the same way and a refusal worded twice is one that will
    drift.
    """
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
    """What kind of thing this column holds, for the purpose of adding to it.

    ``After`` writes ``parent + offset`` into a column, so the parent's column
    and the child's have to be the same kind of thing. They are not
    interchangeable even when Python will add them: ``date + timedelta`` is a
    ``date``, so a ``DateTimeField`` filled from a ``DateField`` parent gets
    dates, ``COPY`` accepts them, and they land as naive midnights.

    ``DateTimeField`` is checked before ``DateField`` because it *subclasses*
    it -- the one ordering in this function that is load-bearing, and the reason
    an ``isinstance`` chain is used rather than a mapping keyed on the class.

    Returns ``None`` for a column this package has no opinion about, and the
    caller then declines to judge rather than inventing a refusal: a custom
    field that adds cleanly to an offset is a legitimate thing to declare, and
    refusing it would cost a caller a working shape to buy nothing.
    """
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
    """Point the identity sequence at the rows the table currently holds.

    Skipping it after a load is the first bug the design invites: rows exist at
    ids 1..N while the sequence still starts at 1, so the very first
    ``objects.create()`` inside a test raises ``IntegrityError`` on a primary key
    that is already taken. Django's own backend operation is used rather than a
    hand-written ``setval`` because it already knows how the column's sequence
    is named.

    **It is computed from the rows, not from a remembered number**, which is
    what makes it correct to run again after a rollback as well as after a load.
    ``setval`` is not transactional, so a scaled world built over a session
    world leaves the counter at the scaled world's size while the session
    world's larger rows come back underneath it -- and the next ``create()``
    then collides with a row the failing test never wrote. Re-running this once
    the transaction has ended reads whatever survived and agrees with it, in
    either direction and with no state carried across the block.
    """
    with connection.cursor() as cursor:
        for statement in connection.ops.sequence_reset_sql(no_style(), [model]):
            cursor.execute(statement)
