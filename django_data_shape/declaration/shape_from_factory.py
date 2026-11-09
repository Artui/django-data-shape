"""Read what a factory actually produces, and say what it does not."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

from django.apps import apps
from django.db import DEFAULT_DB_ALIAS, transaction
from django.db.models import Field, Model, UniqueConstraint

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

    ``defaults`` is passed to every call, because **most factories in a mature
    codebase need arguments**. A ``TeamFactory`` wanting a ``permission_role``,
    called with nothing, leaves a required column empty and fails as a raw
    ``IntegrityError`` naming a constraint -- which says nothing about what the
    caller did. Anything the factory raises is caught and named here instead,
    with the call number, because "it failed" and "it failed after three" are
    different bugs.

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
    arguments = dict(defaults or {})
    with transaction.atomic(using=using):
        for index in range(samples):
            try:
                row = factory(**arguments)
            except Exception as error:
                # Re-raised rather than propagated, because the failure a caller
                # sees otherwise is a database constraint naming a column, which
                # describes neither what they ran nor what to do about it. The
                # driver's own words are kept: unlike a refusal about a
                # declaration, the column their factory left empty is the whole
                # of the information, and nothing here retains a generated value.
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
    """A declaration for one column, and the finding it deserves if any.

    The declaration carries its own trailing comma, **before** any comment: one
    written after ``# every value distinct`` is inside the comment, and the next
    column then has no separator and the source does not compile.
    """
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


def _self_key(
    model: type[Model], field: Field[Any, Any], values: list[object], rows: int
) -> tuple[str | None, str | None]:
    """A declaration for a key to the model's own table, and the finding it deserves.

    Its own function rather than a branch of :func:`_relation`, because the
    answer is a different kind of thing: ``Table`` refuses a self-referential
    ``FanOut`` everywhere, so the suggestion :func:`_relation` makes for every
    other key would hand back source that cannot be built. The key is a
    :class:`~django_data_shape.relations.tree.Tree` where one can fill it.

    **Where this cannot write one, there is no declaration at all** (``None``) and
    the finding says why, rather than a ``Tree`` that will be refused. A column that
    refuses NULL has no first row to point at. A constraint counting every NULL as
    one value holds one root, so its share depends on the rows, which the report
    leaves to the caller. Neither is an error of the factory's, and the rest of what
    it measured is still worth reading, so this does not raise.

    **A unique key gets sizes of one value**, because a tree whose sizes hold one
    value gives no row two children, and that is what such a column requires. Every
    other key gets ``Zipf()``, which the factory did not measure either.

    The root share is measured from ``values``, whose ``None`` is a root and not a
    parent: a ``Counter`` over them would count it as one more parent, as
    :func:`_relation` does for a key to another table, where it is harmless.
    """
    if not field.null:
        return None, (
            "a key that refuses NULL has no first row to point at, so no shape can fill it: a Tree "
            "is refused on this column and a FanOut reads keys from a table still empty at load "
            "time. Make the column null=True, or fill the table with a Projection."
        )
    if _nulls_count_as_one(model, field):
        # The rows a share would be written against are the caller's: the report leaves
        # them as ``rows=...``, so it can name this tree and cannot write it down.
        return None, (
            "a constraint counting every NULL as one value has room for one root, so a Tree "
            "fills it only with one root and sizes of one value, Tree(roots=1/rows, "
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
        # Three figures write a share past 0.9995 as 1, which Tree refuses.
        share = repr(roots / rows)
    why_these_sizes = "a unique key gives no row two children" if unique else "sizes not measured"
    return (
        f"Tree(roots={share}, sizes={sizes}),  # {roots} of {rows} rows had no parent; "
        f"{why_these_sizes}",
        None,
    )


def _is_unique(model: type[Model], field: Field[Any, Any]) -> bool:
    """Whether the column alone is unique, by whichever of the three spellings.

    ``unique=True`` is on the field. A ``UniqueConstraint`` and ``unique_together``
    are on the model, in two different places, and a shape decides a tree under
    each as the unique column it amounts to. A constraint with a condition holds
    only on some rows, so it is not this.
    """
    return (
        field.unique
        or any(_names(model, entry) == (field.name,) for entry in model._meta.unique_together)
        or any(_constraints_on_it_alone(model, field))
    )


def _names(model: type[Model], spelled: Iterable[str]) -> tuple[str, ...]:
    """The fields' names, however each was spelled.

    Django takes ``parent`` and ``parent_id`` in a ``UniqueConstraint`` and in
    ``unique_together`` alike and keeps them as written, so comparing the text to
    ``field.name`` misses the attname's. ``get_field`` answers to either.
    """
    return tuple(model._meta.get_field(name).name for name in spelled)


def _nulls_count_as_one(model: type[Model], field: Field[Any, Any]) -> bool:
    """Whether a constraint on this column alone makes its NULLs one value.

    Read with ``getattr`` because ``nulls_distinct`` arrived in Django 5.0.
    """
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
        if _names(model, constraint.fields) == (field.name,)
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
) -> Iterator[tuple[str, tuple[str, ...]]]:
    """Each uniqueness over this column together with others: how it is named, and its columns."""
    for constraint in _unconditional_constraints(model):
        names = _names(model, constraint.fields)
        if field.name in names and len(names) > 1:
            yield constraint.name, names
    for entry in model._meta.unique_together:
        names = _names(model, entry)
        if field.name in names and len(names) > 1:
            yield f"unique_together {names!r}", names


def _is_distinct(field: Field[Any, Any], values: list[object]) -> bool:
    """Whether the column is written as one distinct in every row, which is ``Sequential``.

    Not a relation, whatever its values: a key to another table is written as a
    fan-out, which is a draw.
    """
    return not field.is_relation and len(set(values)) == len(values)


def _composite_finding(
    model: type[Model],
    field: Field[Any, Any],
    columns: dict[str, tuple[Field[Any, Any], list[object]]],
) -> list[str]:
    """What a tree beside a draw under a composite uniqueness means, one finding per uniqueness.

    **Because ``Shape`` refuses it and the report would not say so.** A pair is
    distinct as soon as either half is, so a column distinct in every row keeps the
    uniqueness on its own and ``Sequential`` is written for it. Where none is, the
    others are drawn per row from a distribution, and siblings of one parent cannot
    see what each other drew.
    """
    findings = []
    for label, names in _composite_uniqueness(model, field):
        others = [name for name in names if name != field.name]
        if any(_is_distinct(*columns[name]) for name in others):
            continue
        drawn = ", ".join(others)
        findings.append(
            f"{label} makes ({', '.join(names)}) unique together, and {drawn} is declared as a "
            "draw rather than as a column distinct in every row. Table refuses a Tree beside a "
            "draw under it, because siblings of one parent cannot see what each other drew. "
            f"Make {drawn} distinct in every row -- Sequential does -- or fill the table with "
            "a Projection with columns= and sql=."
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
        # The comma ends the line for a declaration that has not put it before its
        # comment, which is every one of a key; ``_describe`` has, and a second
        # would sit inside the comment it writes.
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
    """The attribute holding this column's value on an unsaved-or-saved instance.

    ``attname`` rather than ``name`` for a relation, so a foreign key is read as
    the key it stores rather than by fetching the parent -- which would be a
    query per row and would describe the parent instead of this column.
    """
    return field.attname
