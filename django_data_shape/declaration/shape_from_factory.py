from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from django.apps import apps
from django.db import DEFAULT_DB_ALIAS, transaction
from django.db.models import Field, Model, UniqueConstraint

from django_data_shape.declaration.field_names import field_names
from django_data_shape.declaration.invalid_shape import InvalidShape

_MANY_VALUES = 12

# The root share and sizes ``Table``'s own refusal of a self-referential fan-out
# offers, written where the factory gave no share a ``Tree`` accepts.
_OFFERED_ROOTS = 0.05


def shape_from_factory(
    factory: Callable[..., Model],
    *,
    samples: int = 200,
    defaults: Mapping[str, object] | None = None,
    using: str = DEFAULT_DB_ALIAS,
) -> str:
    """Run a factory, measure what it made, and return a declaration **as source**."""
    if samples < 2:
        raise InvalidShape(
            f"shape_from_factory needs at least two samples to see a shape, got {samples}. "
            "One row cannot tell a constant from a distribution."
        )
    before = _counts(using)
    made: list[Model] = []
    arguments = dict(defaults or {})
    with transaction.atomic(using=using):
        for index in range(samples):
            try:
                row = factory(**arguments)
            except Exception as error:
                raise InvalidShape(
                    f"the factory could not be called on call {index + 1} of {samples}: "
                    f"{type(error).__name__}: {error}. Most factories in a real codebase take "
                    "arguments and need them -- pass them with defaults={...}, which is handed "
                    "to every call."
                ) from error
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
            f"Constant({only!r}),",
            f"every row got {only!r}. The factory fixes this column, so the declaration above "
            "says the planner will see one value -- decide what the real spread is.",
        )
    if len(counts) == rows:
        return (
            "Sequential(...),  # every value distinct",
            "every value was distinct. Sequential fills that cheaply; a unique short code needs "
            "a generator of its own.",
        )
    if len(counts) <= _MANY_VALUES:
        shares = ", ".join(
            f"{value!r}: {count / rows:.2f}" for value, count in counts.most_common()
        )
        return f"Skew({{{shares}}}),", None
    return (
        f"Skew({{...}}),  # {len(counts)} distinct values, "
        f"top share {counts.most_common(1)[0][1] / rows:.0%}",
        f"{len(counts)} distinct values is more than a Skew wants written out. If they are "
        "evenly spread, Uniform says it in one line; if they are not, keep the head and let the "
        "tail share a weight.",
    )


def _relation(values: list[object], rows: int) -> tuple[str, str | None]:
    """A declaration for a foreign key, which is a fan-out and never a draw."""
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
        f"FanOut(Zipf()),  # {parents} parents, {spread[0]} at the head and {spread[-1]} at the "
        "tail",
        None,
    )


def _self_key(
    model: type[Model], field: Field[Any, Any], values: list[object], rows: int
) -> tuple[str | None, str | None]:
    """A declaration for a key to the model's own table, and the finding it deserves."""
    if not field.null:
        return None, (
            "a key that refuses NULL has no first row to point at, so no shape can fill it: a Tree "
            "is refused on this column and a FanOut reads keys from a table still empty at load "
            "time. Make the column null=True, or fill the table with a Projection."
        )
    if _nulls_count_as_one(model, field):
        return None, (
            "a constraint counting every NULL as one value has room for one root, so a Tree "
            "fills it only with one root and sizes of one value, Tree(roots=1/(rows + 1), "
            "sizes=Constant(1)) at the rows you build, and the share a factory measures is no "
            "use. Leaving it out holds one NULL many times. Write that tree, or fill the table "
            "with a Projection."
        )
    unique = _is_unique(model, field)
    sizes = "Constant(1)" if unique else "Zipf()"
    roots = sum(value is None for value in values)
    if roots in (0, rows):
        unmeasured = "roots not measured" if unique else "roots and sizes not measured"
        why_these_sizes = (
            f"sizes={sizes} is the one spread a unique key holds"
            if unique
            else f"sizes={sizes} is not measured either"
        )
        return (
            f"Tree(roots={_OFFERED_ROOTS}, sizes={sizes}),  # {unmeasured}",
            ("every row was left without a parent" if roots else "no row was left without a parent")
            + f" over {rows} calls, so there is no root share to write down: a tree's first "
            "row is always a root, and Tree takes a share strictly between 0 and 1. "
            f"roots={_OFFERED_ROOTS} is the figure Table's own refusal offers, not measured, "
            f"and {why_these_sizes}.",
        )
    share = f"{roots / rows:.3g}"
    if not 0 < float(share) < 1:
        share = repr(roots / rows)
    why_these_sizes = "a unique key gives no row two children" if unique else "sizes not measured"
    return (
        f"Tree(roots={share}, sizes={sizes}),  # {roots} of {rows} rows had no parent; "
        f"{why_these_sizes}",
        None,
    )


def _is_unique(model: type[Model], field: Field[Any, Any]) -> bool:
    """Whether the column alone is unique, by whichever of the three spellings."""
    return (
        field.unique
        or any(field_names(model, entry) == (field.name,) for entry in model._meta.unique_together)
        or any(_constraints_on_it_alone(model, field))
    )


def _nulls_count_as_one(model: type[Model], field: Field[Any, Any]) -> bool:
    """Whether a constraint on this column alone makes its NULLs one value."""
    return any(
        getattr(constraint, "nulls_distinct", None) is False
        for constraint in _constraints_on_it_alone(model, field)
    )


def _constraints_on_it_alone(
    model: type[Model], field: Field[Any, Any]
) -> Iterator[UniqueConstraint]:
    """The unconditional ``UniqueConstraint``s over this one column and nothing else."""
    return (
        constraint
        for constraint in _unconditional_constraints(model)
        if field_names(model, constraint.fields) == (field.name,)
    )


def _unconditional_constraints(model: type[Model]) -> Iterator[UniqueConstraint]:
    """The ``UniqueConstraint``s that hold on every row, which is what a shape must keep."""
    return (
        constraint
        for constraint in model._meta.constraints
        if isinstance(constraint, UniqueConstraint) and constraint.condition is None
    )


def _composite_uniqueness(
    model: type[Model], field: Field[Any, Any]
) -> Iterator[tuple[str, tuple[str, ...], bool]]:
    """Each uniqueness over this column with others, and whether it keeps each NULL apart."""
    for constraint in _unconditional_constraints(model):
        names = field_names(model, constraint.fields)
        if field.name in names and len(names) > 1:
            yield (
                constraint.name,
                names,
                getattr(constraint, "nulls_distinct", None) is not False,
            )
    for entry in model._meta.unique_together:
        names = field_names(model, entry)
        if field.name in names and len(names) > 1:
            yield f"unique_together {names!r}", names, True


def _is_distinct(field: Field[Any, Any], values: list[object]) -> bool:
    """Whether the column is written as one distinct in every row, which is ``Sequential``."""
    return not field.is_relation and len(set(values)) == len(values)


def _composite_finding(
    model: type[Model],
    field: Field[Any, Any],
    columns: dict[str, tuple[Field[Any, Any], list[object]]],
) -> list[str]:
    """What a tree beside a draw under a composite uniqueness means, per uniqueness."""
    findings = []
    unique = _is_unique(model, field)
    for label, names, apart in _composite_uniqueness(model, field):
        if unique and apart:
            continue
        others = [name for name in names if name != field.name]
        if any(_is_distinct(*columns[name]) for name in others):
            continue
        keys = [name for name in others if columns[name][0].is_relation]
        plain = [name for name in others if not columns[name][0].is_relation]
        why = [
            *([f"{', '.join(keys)} is a fan-out, which draws its parent per row"] if keys else []),
            *(
                [
                    f"{', '.join(plain)} is declared as a draw rather than as a column distinct "
                    "in every row"
                ]
                if plain
                else []
            ),
        ]
        remedies = [
            *(
                [f"give {', '.join(keys)} max_children=1, which gives no parent two rows"]
                if keys
                else []
            ),
            *(
                [f"make {', '.join(plain)} distinct in every row (Sequential does)"]
                if plain
                else []
            ),
        ]
        advice = "; ".join(
            [*remedies, "or fill the table with a Projection with columns= and sql="]
        )
        findings.append(
            f"{label} makes ({', '.join(names)}) unique together, and {' and '.join(why)}. "
            "Shape refuses a Tree beside a draw under it, because siblings of one parent cannot "
            f"see what each other drew. {advice[0].upper()}{advice[1:]}."
        )
    return findings


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

    columns = {
        field.name: (field, [getattr(row, _read(field)) for row in made])
        for field in model._meta.concrete_fields
        if not field.primary_key
    }
    body = [f"Table({model.__name__}, rows=...,"]
    for field, values in columns.values():
        declaration: str | None
        # The comma goes before any comment, or it lands inside the comment it writes.
        comma = ","
        extra: list[str] = []
        if field.is_relation and field.related_model is model:
            declaration, finding = _self_key(model, field, values, len(made))
            if declaration is not None:
                extra = _composite_finding(model, field, columns)
        elif field.is_relation:
            declaration, finding = _relation(values, len(made))
        else:
            declaration, finding = _describe(values, len(made))
            comma = ""
        if declaration is None:
            body.append(f"    # {field.name}: nothing written here -- see the findings above.")
        else:
            body.append(f"    {field.name}={declaration}{comma}")
        findings.extend(
            f"{model.__name__}.{field.name}: {text}" for text in [finding, *extra] if text
        )
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
    """The attribute holding this column's value on an unsaved-or-saved instance."""
    return field.attname
