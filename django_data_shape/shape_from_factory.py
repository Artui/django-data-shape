"""Read what a factory actually produces, and say what it does not."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

from django.apps import apps
from django.db import DEFAULT_DB_ALIAS, transaction
from django.db.models import Field, Model

from django_data_shape.invalid_shape import InvalidShape

_MANY_VALUES = 12


def shape_from_factory(
    factory: Callable[[], Model],
    *,
    samples: int = 200,
    using: str = DEFAULT_DB_ALIAS,
) -> str:
    """Run a factory, measure what it made, and return a declaration **as source**.

    **Source to read, edit and check in -- never a ``Shape`` to build from**, and
    that is the whole design rather than a limitation of it. A shape this package
    builds is *declared*, which is what makes it reviewable and assertable; a
    shape learned from a sample is neither, and one used directly would change
    whenever the factory did, silently. So what comes back is text, and a person
    decides what of it to keep.

    **Because the thing it usually finds is that the factory is flat.** Factories
    are written for single-object tests, so they fix values: one status, one
    parent, one count. Measured on exactly that shape, faithful inference emits
    ``status=Constant('ACTIVE'), company=Constant(1)`` -- the uniform world this
    package exists to argue against, now with a declaration blessing it. So every
    such column is reported as a **finding** rather than quietly written down,
    and the report leads with them.

    **A sub-factory is the sharpest case and the one worth running this for.**
    ``company = SubFactory(CompanyFactory)`` creates one parent per child, which
    is a fan-out of degree one: every parent has exactly one row, the average is
    the truth, and the join estimate cannot miss. It is the single most
    unrealistic thing a fixture can do and it is invisible in the factory's own
    source, so it is detected by watching which other tables grew and by how
    much.

    Nothing is left behind: the calls run inside a transaction that is rolled
    back, so this can be pointed at a development database without writing to it.

    ``samples`` decides how much of the tail is seen, and the answer moves with
    it -- fifty runs saw nineteen distinct parents where a thousand saw
    forty-nine. That is stated in the output rather than hidden, because two
    people running this at different sizes should not be surprised by two
    different declarations.
    """
    if samples < 2:
        raise InvalidShape(
            f"shape_from_factory needs at least two samples to see a shape, got {samples}. "
            "One row cannot tell a constant from a distribution."
        )
    before = _counts(using)
    made: list[Model] = []
    with transaction.atomic(using=using):
        for _ in range(samples):
            row = factory()
            if not isinstance(row, Model):
                raise InvalidShape(
                    f"shape_from_factory expects the factory to return a model instance, got "
                    f"{type(row).__name__}. A factory that returns nothing cannot be measured, "
                    "because what it made is what is being read."
                )
            made.append(row)
        after = _counts(using)
        transaction.set_rollback(True, using=using)
    return _render(made[0].__class__, made, _grown(before, after, made[0].__class__), samples)


def _counts(using: str) -> dict[type[Model], int]:
    """How many rows every model holds, so growth elsewhere can be seen."""
    return {
        model: model._base_manager.using(using).count()
        for config in apps.get_app_configs()
        for model in config.get_models()
        if not model._meta.proxy
    }


def _grown(
    before: dict[type[Model], int], after: dict[type[Model], int], made: type[Model]
) -> dict[type[Model], int]:
    return {
        model: after[model] - before[model]
        for model in after
        if model is not made and after[model] != before.get(model, 0)
    }


def _describe(values: list[object], rows: int) -> tuple[str, str | None]:
    """A declaration for one column, and the finding it deserves if any."""
    counts = Counter(values)
    if len(counts) == 1:
        only = next(iter(counts))
        return (
            f"Constant({only!r})",
            f"every row got {only!r}. The factory fixes this column, so the declaration above "
            "says the planner will see one value -- decide what the real spread is.",
        )
    if len(counts) == rows:
        return (
            "Sequential(...)  # every value distinct",
            "every value was distinct. Sequential fills that cheaply; a unique short code needs "
            "a generator of its own.",
        )
    if len(counts) <= _MANY_VALUES:
        shares = ", ".join(
            f"{value!r}: {count / rows:.2f}" for value, count in counts.most_common()
        )
        return f"Skew({{{shares}}})", None
    return (
        f"Skew({{...}})  # {len(counts)} distinct values, "
        f"top share {counts.most_common(1)[0][1] / rows:.0%}",
        f"{len(counts)} distinct values is more than a Skew wants written out. If they are "
        "evenly spread, Uniform says it in one line; if they are not, keep the head and let the "
        "tail share a weight.",
    )


def _relation(values: list[object], rows: int) -> tuple[str, str | None]:
    """A declaration for a foreign key, which is a fan-out and never a draw.

    Its own function rather than a branch in :func:`_describe`, because the
    answer is a different *kind* of thing. ``Table`` refuses a value
    distribution on a relation -- it would emit keys drawn from nothing -- so
    emitting one here would hand back source that cannot be built, which is a
    worse failure than a wrong number: the reader would take it for a starting
    point and it is not even a valid one.

    The finding is what matters anyway. A factory reaches a foreign key in one
    of two unrealistic ways and both come out here: a sub-factory gives every
    parent exactly one child, and a round-robin gives every parent the same
    number. Either way the average is the truth and a join over that column
    cannot be misestimated, which is the defect this package exists to make
    reproducible.
    """
    counts = Counter(values)
    spread = sorted(counts.values(), reverse=True)
    parents = len(counts)
    if parents == 1:
        return (
            "FanOut(Zipf()),  # every row pointed at one parent",
            "every row pointed at the same parent. That is not a fan-out at all -- the column has "
            "one value, so the planner sees no distribution across the join.",
        )
    if spread[0] == spread[-1]:
        return (
            f"FanOut(Zipf()),  # was flat: {parents} parents, {spread[0]} row(s) each",
            f"every one of the {parents} parents got exactly {spread[0]} row(s). A flat fan-out "
            "is the one shape in which a join estimate cannot miss, because the average is the "
            "truth -- declare the tail you actually have, and childless= for the parents nobody "
            "references.",
        )
    return (
        f"FanOut(Zipf()),  # {parents} parents, {spread[0]} at the head and {spread[-1]} at the tail",
        None,
    )


def _render(
    model: type[Model], made: list[Model], grown: dict[type[Model], int], samples: int
) -> str:
    lines = [
        f"# Measured from {samples} calls. This is source to read and edit, not a shape to",
        "# build from: what it found is what your factory does, which is not the same",
        "# question as what your production data looks like.",
        "",
    ]
    findings: list[str] = []
    for model_grown, delta in sorted(grown.items(), key=lambda pair: pair[0].__name__):
        if delta == samples:
            findings.append(
                f"{model_grown.__name__} grew by exactly one per call -- a sub-factory. That is a "
                "fan-out of degree one: every parent has exactly one child, so the average is the "
                "truth and a join over it cannot be misestimated. It is the most unrealistic "
                "thing a fixture can do, and nothing in the factory's source says so."
            )
        else:
            findings.append(
                f"{model_grown.__name__} grew by {delta} over {samples} calls, about "
                f"{samples / delta:.1f} children per parent."
            )

    body = [f"Table({model.__name__}, rows=...,"]
    for field in model._meta.concrete_fields:
        if field.primary_key:
            continue
        values = [getattr(row, _read(field)) for row in made]
        if field.is_relation:
            declaration, finding = _relation(values, len(made))
        else:
            declaration, finding = _describe(values, len(made))
        body.append(f"    {field.name}={declaration},")
        if finding:
            findings.append(f"{model.__name__}.{field.name}: {finding}")
    body.append(")")

    if findings:
        lines.append("# What your factory does not vary, which is what this is for:")
        lines.extend(f"#   - {finding}" for finding in findings)
        lines.append("")
    lines.extend(body)
    lines.append("")
    lines.append(
        f"# Read at {samples} samples. A larger run sees more of the tail, so the numbers above "
        "move with it."
    )
    return "\n".join(lines)


def _read(field: Field[Any, Any]) -> str:
    """The attribute holding this column's value on an unsaved-or-saved instance.

    ``attname`` rather than ``name`` for a relation, so a foreign key is read as
    the key it stores rather than by fetching the parent -- which would be a
    query per row and would describe the parent instead of this column.
    """
    return field.attname
