"""Refusing a declaration the schema's own constraints could not hold."""

from __future__ import annotations

from typing import Any, cast

from django.db.models import Model, Q, UniqueConstraint

from django_data_shape.derivations.per_parent import PerParent
from django_data_shape.distributions.bounded import Bounded
from django_data_shape.distributions.categorical import Categorical
from django_data_shape.fan_out import FanOut
from django_data_shape.invalid_shape import InvalidShape
from django_data_shape.projection import Projection
from django_data_shape.table import Table


def check_constraints(tables: tuple[Table | Projection, ...]) -> None:
    """Refuse a shape whose row counts contradict the models' own constraints.

    The third net -- the database itself -- catches every one of these, and
    catches it with a terrible message: a unique index failing at row 700,000 of
    a load that has already run for a minute, naming an index rather than a
    declaration. So the arithmetic is done here instead, from
    ``Model._meta.constraints``, before a single row is generated.

    **``_meta.total_unique_constraints`` is the helper that sounds right and
    is the wrong one.** It deliberately excludes conditional constraints -- it
    exists to answer whether a relation is one-to-one, and a constraint that
    only sometimes applies cannot answer that -- so it skips exactly the case
    this function is for. ``_meta.constraints`` is read directly.

    Three checks, and they differ in how certain they are and in what they are
    counting.

    **An unconditional ``UniqueConstraint`` is pigeonhole**, and provable. Two
    million rows needing distinct ``(company, label)`` pairs cannot be built
    from fifty thousand companies and three labels, whatever the seed. That is
    also the multi-column analysis
    :meth:`~django_data_shape.table.Table._check_satisfiable` declines to
    attempt, and it declines for a good reason: a table alone does not know how
    many companies there are. A whole shape does, which is why this runs here.

    **Enough room is not a way to fill it**, which is the second check and the
    one no arithmetic reaches. An unconditional constraint over two fan-outs --
    the through table of a many-to-many -- passes the pigeonhole comfortably,
    because the product of two parent counts dwarfs the row count, and still
    cannot be built: two partitions of the same rows are computed independently,
    so the pairs they produce are an artefact of the row index and a collision
    is a matter of the seed. It is refused rather than counted.

    **A conditional ``UniqueConstraint`` is a statement about a group**, and the
    refusal is categorical rather than arithmetic. ``one_active_project_per_company``
    permits one ``ACTIVE`` row per company; a ``Skew`` filling ``status`` draws
    each row independently, so it cannot keep a per-group rule *at any weight* --
    2.5% is as broken as 10%, just later in the load. The arithmetic goes in the
    message because it is what makes the refusal legible, not because it is what
    decides it. The remedy is
    :class:`~django_data_shape.derivations.per_parent.PerParent`, which makes
    the count one per non-empty group and therefore derived from the fan-out
    rather than chosen beside it.

    **What this cannot decide, and leaves to the other two nets.** A condition
    that is not a single equality -- ``Q(status__in=[...])``, a negation, two
    clauses -- is skipped, because reading it would mean this package deciding
    what an arbitrary predicate matches. A constraint over ``expressions``
    rather than ``fields`` is skipped for the same reason. A conditional
    constraint whose grouping columns include no declared ``FanOut`` is skipped
    because there is no partition here to satisfy it with, so a refusal would
    name no remedy. A column filled by a distribution that cannot enumerate
    itself -- anything not
    :class:`~django_data_shape.distributions.categorical.Categorical` -- is
    undecidable, and so is a fan-out over a parent this shape does not build,
    or one carrying a null share, because PostgreSQL counts each NULL as its own
    group and those rows are exempt from the index. In every one of those cases
    the declaration is allowed through and the post-load check and the database
    are what catch it.

    A :class:`~django_data_shape.projection.Projection` is skipped entirely. Its
    columns are copied along a join rather than drawn from distributions, so
    there is no declared share to compare a capacity against.
    """
    rows_of: dict[type[Model], int] = {
        table.model: table.rows for table in tables if isinstance(table, Table)
    }
    for table in tables:
        if isinstance(table, Projection):
            continue
        for constraint in table.model._meta.constraints:
            if isinstance(constraint, UniqueConstraint) and constraint.fields:
                _check_one(table, constraint, rows_of)


def _check_one(table: Table, constraint: UniqueConstraint, rows_of: dict[type[Model], int]) -> None:
    fields = tuple(constraint.fields)
    capacity = _capacity(table, fields, rows_of)
    if constraint.condition is None:
        _check_unconditional(table, constraint.name, fields, capacity)
        _check_independent_fan_outs(table, constraint.name, fields)
        return
    decoded = _equality(constraint.condition)
    if decoded is None:
        return
    column, value = decoded
    declared = table.fields.get(column)
    grouped_by_fan_out = any(isinstance(table.fields.get(name), FanOut) for name in fields)
    if declared is None or not grouped_by_fan_out:
        return
    _check_conditional(table, constraint.name, fields, capacity, column, value, declared)


def _check_unconditional(
    table: Table, name: str, fields: tuple[str, ...], capacity: int | None
) -> None:
    """Pigeonhole: distinct combinations against rows, when both are known."""
    if capacity is None or capacity >= table.rows:
        return
    raise InvalidShape(
        f"{name} needs {table.rows} distinct ({', '.join(fields)}) combinations, one per row of "
        f"{table.model.__name__}, and this shape can produce {capacity}. Two rows would have to "
        "share a combination, so the database refuses the load however the seed falls. Widen a "
        "distribution, build more parents, or lower rows."
    )


def _check_independent_fan_outs(table: Table, name: str, fields: tuple[str, ...]) -> None:
    """Two fan-outs over one constraint: enough room, and nothing to arrange it.

    The through table of a many-to-many, which is the shape this reaches first
    and the one it was written for. ``Membership(project, person)`` is unique on
    the pair, both columns are relations, so both are fan-outs -- and the
    pigeonhole check above passes it happily, because fifty projects and two
    hundred people leave ten thousand pairs for five hundred rows.

    Room is not the question. A fan-out is a **partition of this table's rows
    over one parent's keys**, computed from the row index and from nothing else,
    which is what lets a child's foreign key be satisfied with no lookup and no
    global solve. Two of them partition the same rows independently and neither
    can see the other's assignment, so which pairs come out together is an
    artefact of the row index. Whether two rows land on the same pair is then a
    matter of the seed, and the load dies inside ``COPY`` at a row number that
    moves when the seed does.

    That is why this is a refusal and not a wider capacity calculation: no
    arithmetic over the two marginals decides it, because the failure is that
    nothing enumerates the combinations at all. Deduplicated edges are a
    different algorithm rather than a bigger loop, and until they arrive the
    thing that works is a statement --
    :class:`~django_data_shape.projection.Projection` with ``columns=`` and
    ``sql=`` fills the table from a select that can deduplicate.
    """
    fanned = sorted(name for name in fields if isinstance(table.fields.get(name), FanOut))
    if len(fanned) < 2:
        return
    where = table.model.__name__
    raise InvalidShape(
        f"{name} needs every ({', '.join(fields)}) combination distinct, and "
        f"{', '.join(f'{where}.{column}' for column in fanned)} are fan-outs. A fan-out is a "
        "partition of this table's rows over one parent's keys, computed from the row index "
        "alone, so two of them partition the same rows without either seeing the other: which "
        "pairs come out together is an artefact of that index, and two rows sharing a pair is a "
        "matter of the seed. The combinations do fit, which is why the pigeonhole arithmetic "
        "lets this through -- what is missing is anything that enumerates them, and the load "
        "then fails inside COPY at a row number that moves when the seed does. A deduplicated "
        f"edge table is filled by a statement rather than by two draws: Projection({where}, "
        "columns=(...), sql=...) selects the pairs already distinct, which is the form that "
        "keeps this constraint today."
    )


def _check_conditional(
    table: Table,
    name: str,
    fields: tuple[str, ...],
    capacity: int | None,
    column: str,
    value: object,
    declared: object,
) -> None:
    """One row per group may carry the condition's value. Who is filling it?"""
    where = f"{table.model.__name__}.{column}"
    group = ", ".join(fields)
    if isinstance(declared, PerParent):
        if declared.relation not in fields:
            raise InvalidShape(
                f"{where} is decided per group of {declared.relation!r}, but {name} groups by "
                f"({group}). A rule kept once per {declared.relation} says nothing about how "
                f"many rows with {column}={value!r} a ({group}) ends up with. Group the "
                "PerParent by one of the constraint's own fields."
            )
        if declared.special == value:
            if declared.count > 1:
                raise InvalidShape(
                    f"{where} puts {value!r} on {declared.count} rows of every group, and {name} "
                    "permits one. count= is what N-winners-per-contest is for, and a unique "
                    "constraint is the case it is not."
                )
            return
        # The special value is a different one, so the constraint's value can
        # only arrive through the rest of the group -- which PerParent has
        # already narrowed to a plain value or a distribution that enumerates
        # itself, so this is decidable either way.
        if not _produces(declared.rest, value):
            return
    elif _produces(declared, value) is False:
        return

    permits = (
        f"at most {capacity} rows with {column}={value!r}, one per ({group})"
        if capacity is not None
        else f"at most one row with {column}={value!r} per ({group})"
    )
    asks = _asks(table, declared, value)
    raise InvalidShape(
        f"{name} permits {permits}; {where} is filled by {declared!r}, which {asks}. A rule about "
        "a group cannot be kept by a draw made per row, at any weight -- a smaller share only "
        "moves the collision later into the load. Declare the column with "
        f"PerParent({fields[0]!r}, last={value!r}, rest=...), which puts it on one row of each "
        "group and makes the count derived from the fan-out rather than chosen beside it."
    )


def _asks(table: Table, declared: object, value: object) -> str:
    """The declaration's own arithmetic, quoted back at it when it has any."""
    if isinstance(declared, Categorical):
        share = next((s for v, s in declared.shares().items() if v == value), 0.0)
        return f"asks for {round(table.rows * share)} of them"
    return f"draws {value!r} independently per row"


def _capacity(table: Table, fields: tuple[str, ...], rows_of: dict[type[Model], int]) -> int | None:
    """How many distinct combinations of ``fields`` this declaration can build.

    None where any one of them cannot be counted, because a capacity that
    guessed low would refuse a shape that builds perfectly well -- and a refusal
    that is sometimes wrong is worse than the load failure it replaces.
    """
    total = 1
    for name in fields:
        declared = table.fields.get(name)
        if isinstance(declared, FanOut):
            # A null share is not a smaller capacity, it is an unbounded one:
            # PostgreSQL treats every NULL in a unique index as distinct, so
            # those rows are exempt from the constraint entirely.
            if declared.null:
                return None
            parent = cast("type[Model]", table.model._meta.get_field(name).related_model)
            parent_rows = rows_of.get(parent)
            if parent_rows is None:
                return None
            total *= parent_rows
        elif isinstance(declared, Bounded):
            total *= declared.distinct_values()
        else:
            return None
    return total


def _produces(declared: object, value: object) -> bool | None:
    """Whether this declaration can put ``value`` in a row: yes, no, or unknown.

    ``False`` is the only answer that lets a constraint through, and it is only
    ever given by a declaration that enumerated itself. A ``Constant`` of
    another value and a ``Skew`` that never lists this one are both provably
    safe; a ``Uniform`` says nothing about what it emits, and unknown is read as
    a yes -- the refusal is what this is deciding, and a refusal must never be
    the one that is wrong.

    ``PerParent``'s ``rest`` reaches here too, and it is either a plain value or
    a ``Categorical``, so for that caller the answer is never unknown.
    """
    if isinstance(declared, Categorical):
        return any(candidate == value for candidate in declared.shares())
    return None if _is_declaration(declared) else bool(declared == value)


def _is_declaration(declared: object) -> bool:
    """Whether this is a distribution rather than a plain column value.

    The same structural test
    :func:`~django_data_shape.derivations.per_parent.PerParent` makes about its
    own ``rest``, and made on the **type** for the same reason: an ``Enum``
    member carries a ``value`` and is an entirely ordinary thing to fill a
    status column with, while the descriptor behind it is not callable.
    """
    return callable(getattr(type(declared), "value", None))


def _equality(condition: Q) -> tuple[str, object] | None:
    """A condition of the form ``Q(column=value)``, or None for anything else.

    Only the single, un-negated equality is read. Everything else -- ``__in``, a
    negation, two clauses joined -- would mean this package deciding which rows
    an arbitrary predicate matches, which is the database's job and not a job
    worth half-doing.
    """
    if condition.negated or len(condition.children) != 1:
        return None
    child: Any = condition.children[0]
    if not isinstance(child, tuple) or len(child) != 2:
        return None
    lookup, value = child
    column, _, suffix = str(lookup).partition("__")
    if suffix not in ("", "exact"):
        return None
    return column, value
