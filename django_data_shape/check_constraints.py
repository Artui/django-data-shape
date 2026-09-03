"""Refusing a declaration the schema's own constraints could not hold."""

from __future__ import annotations

from typing import Any, cast

from django.db.models import Model, Q, UniqueConstraint

from django_data_shape.derivations.derivation import Derivation
from django_data_shape.derivations.per_parent import PerParent
from django_data_shape.distributions.bounded import Bounded
from django_data_shape.distributions.categorical import Categorical
from django_data_shape.distributions.distinct import Distinct
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
        # Order is the whole of what keeps the arithmetic reachable. All three
        # can apply to one declaration, and the pigeonhole is the most useful of
        # them: "this shape does not fit at all" is a different instruction from
        # "it fits and nothing arranges it". Moving either arrangement check
        # above it would answer the wrong question first and, under a 100%
        # branch gate with no pragma, would take the 0.8.0 message down with it.
        _check_unconditional(table, constraint.name, fields, capacity)
        _check_independent_fan_outs(table, constraint.name, fields, rows_of)
        _check_a_fan_out_beside_a_draw(table, constraint.name, fields, capacity, rows_of)
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


def _check_independent_fan_outs(
    table: Table, name: str, fields: tuple[str, ...], rows_of: dict[type[Model], int]
) -> None:
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

    **Unless one of the two partitions has no group of two**, which is the
    exemption below and is a proof rather than a probability: such a fan-out
    never repeats a parent key, so no two rows share that column and the pair is
    distinct on that half alone. See :func:`_cannot_collide`.

    That is why this is otherwise a refusal and not a wider capacity
    calculation: no arithmetic over the two marginals decides it, because the
    failure is that nothing enumerates the combinations at all. Deduplicated edges are a
    different algorithm rather than a bigger loop, and until they arrive the
    thing that works is a statement --
    :class:`~django_data_shape.projection.Projection` with ``columns=`` and
    ``sql=`` fills the table from a select that can deduplicate.
    """
    fanned = sorted(name for name in fields if isinstance(table.fields.get(name), FanOut))
    if len(fanned) < 2:
        return
    # **One** of them is enough, not both, and that is the whole of the
    # exemption here. A fan-out that gives no parent two rows never repeats a
    # key, so no two rows share that column at all -- and a pair is distinct as
    # soon as either half is, whatever the other fan-out does with it. Measured
    # rather than reasoned into: twenty rows over twenty companies partitioned
    # flat, beside a Zipf over five people, loads twenty times out of twenty,
    # and the person fan-out cannot possibly satisfy the proof at those numbers.
    # Asking both to satisfy it would refuse that shape, which is the wrongness
    # this exemption exists to remove rather than a stricter version of it.
    if any(_cannot_collide(table, column, rows_of) for column in fanned):
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


def _check_a_fan_out_beside_a_draw(
    table: Table,
    name: str,
    fields: tuple[str, ...],
    capacity: int | None,
    rows_of: dict[type[Model], int],
) -> None:
    """One fan-out and one drawn column: the same defect, one column over.

    ``Seat(company, label)`` unique on the pair, with ``company=FanOut(Zipf())``
    and ``label=Skew({"a": 1, "b": 1})`` over fifty companies. At two thousand
    rows the pigeonhole above refuses it and quotes the arithmetic. At a hundred
    rows the capacity is exactly a hundred, every check passes, and the load
    dies inside ``COPY`` on the unique index -- at row 17 for one seed and
    somewhere else for the next.

    **The proof is the same one
    :func:`_check_independent_fan_outs` makes, and it does not depend on the
    second column being a partition.** A
    :class:`~django_data_shape.distributions.distribution.Distribution` is by
    contract a pure function of the row index and of a draw derived from the
    field name and that same index -- that is what lets rows be emitted in an
    order different from the one they were assigned in, and it is stated on the
    protocol rather than assumed here. So a drawn column cannot see which parent
    the fan-out gave its row any more than a second fan-out could, nothing
    enumerates the pairs *within a group*, and two rows sharing one is a matter
    of the seed. Room is not the question here either: a group of seven rows
    over two labels collides whatever the table's total capacity says.

    **No arithmetic decides it**, which is why this is a refusal rather than a
    wider capacity calculation. The quantity that would decide it is the largest
    group the fan-out produces, and that is not known until the partition is
    resolved against the parent's real keys at build time -- by which point the
    declaration has already been accepted, cached and passed around.

    **Two exemptions, and both are proofs rather than probabilities.**
    ``Distinct`` is the first: a pair is distinct as soon as either half is, so a
    :class:`~django_data_shape.distributions.sequential.Sequential` beside a
    fan-out keeps the constraint in every row with nothing arranged around it. A
    partition that gives no parent two rows is the second -- there is then no
    group for two draws to collide inside -- and :func:`_cannot_collide` is the
    four conditions that establish it. Neither exemption is "this usually works":
    a shape that merely usually works is the case this refusal is for, and the
    measured ones sit at ten and eleven times out of twenty.

    **What this does not decide.** A column filled by a
    :class:`~django_data_shape.derivations.derivation.Derivation` is left alone,
    and that is the point of the exemption rather than a gap in it: a derivation
    reads something other than its own row index, so it is the one kind of
    declaration that *can* be arranged around a group --
    ``Derived("company", compute=..., scope="group")`` receives this row's
    position inside its parent's children and can hand back a value per
    position. Whether a particular ``compute`` actually does is not readable
    here, and refusing on a callable this package cannot read would be the
    refusal that is wrong. A column the shape leaves undeclared is skipped for
    the reason PostgreSQL skips it: it is nullable and holds NULL, and each NULL
    in a unique index is its own group.
    """
    fanned = [column for column in fields if isinstance(table.fields.get(column), FanOut)]
    others = [column for column in fields if column not in fanned]
    declared = [table.fields.get(column) for column in others]
    # One condition rather than three, because it is one question: is this the
    # shape of declaration this refusal is about? Two fan-outs are the check
    # above and answer with a different remedy, none at all is a constraint no
    # partition groups, and a derivation or an undeclared column is the
    # exemption the docstring gives.
    if len(fanned) != 1 or not declared or not all(_is_drawn_per_row(one) for one in declared):
        return
    if any(isinstance(one, Distinct) and one.is_distinct_per_row() for one in declared):
        return
    if _cannot_collide(table, fanned[0], rows_of):
        return
    where = table.model.__name__
    drawn = ", ".join(f"{where}.{column}={table.fields[column]!r}" for column in others)
    room = (
        f"The {capacity} combinations do fit the {table.rows} rows"
        if capacity is not None
        else "There may be room for every row"
    )
    raise InvalidShape(
        f"{name} needs every ({', '.join(fields)}) combination distinct, and {where}."
        f"{fanned[0]} is a fan-out beside {drawn}. A fan-out is a partition of this table's rows "
        "over one parent's keys, computed from the row index alone, and a distribution is drawn "
        "from that same index and nothing else -- so neither column can see what the other put "
        "in the row, and nothing enumerates the combinations inside a group. Whether two of one "
        f"parent's rows draw the same value is a matter of the seed. {room}, which is why the "
        "pigeonhole arithmetic lets this through, and the load then fails inside COPY at a row "
        "number that moves when the seed does. A value that varies with the group is derived "
        f"from it rather than drawn beside it: Derived({fanned[0]!r}, compute=..., scope='group') "
        "receives this row's position among its parent's children and how many there are. A "
        "column that is distinct in every row keeps the constraint on its own, and says so with "
        "Distinct -- Sequential does."
    )


def _cannot_collide(table: Table, column: str, rows_of: dict[type[Model], int]) -> bool:
    """Whether this fan-out provably gives no parent two rows to draw for.

    The second exemption, and it is a **proof rather than a probability**, which
    is the line this refusal is drawn on. A collision under a constraint like
    this one is always two rows of the *same* group drawing the same value, so a
    partition in which no group holds two rows cannot produce one, whatever the
    other column draws and whatever the seed.

    Four conditions, and all four are needed. The sizes have to be provably flat
    -- ``Bounded`` with exactly one distinct value, so every parent is weighed
    the same -- because ``_sizes`` divides ``rows`` by a total and hands the
    remainder to the parents with the largest fractional part: flat weights make
    every share ``rows / parents``, so at ``rows <= parents`` every parent gets
    zero or one and at one row more some parent gets two. ``childless`` has to be
    zero, because a childless parent is weighed at zero and its rows go to the
    others, which is what breaks the bound rather than tightening it -- measured
    at ``childless=0.1`` and fifty rows over fifty parents, where the largest
    group is two. And the parent's row count has to be known here, which means
    the parent is declared in this same shape: a parent loaded by the caller has
    however many rows it has, and a bound resting on a number this package
    cannot read is not a bound.

    **The asymmetry is worth stating.** Everywhere else in this module a
    ``Bounded`` that lied would cost a refusal that is wrong; here it costs an
    acceptance that is, and the load would die inside ``COPY`` again. That is the
    same exposure every use of the protocol carries and the package cannot do
    better than the contract -- but it is the reason this asks for a proof and
    then asks for three more things beside it, rather than reading one number and
    trusting the shape of the answer.

    Worth knowing what is being exempted: a fan-out giving every parent exactly
    one child is the uniform fan-out this package exists to argue against, and it
    is the one database in which join misestimation cannot occur. That is a
    reason to say so in the documentation. It is never a reason for a refusal to
    tell a caller their shape cannot be built when it demonstrably can.

    .. warning::

       **The coverage gate cannot see these four conditions, and the tests are
       what hold them.** They are one boolean expression, so they are one branch
       arc: drop any single conjunct and the suite still reports 100% line and
       branch, because the surviving expression is still taken both ways. The
       guard against that is one test per condition, and each has been falsified
       against a tree with that condition removed. If you edit this expression,
       edit those with it -- they are, in order:

       - ``Bounded`` with one value -- ``test_sizes_that_are_not_provably_flat_are_not_exempt``,
         which uses ``Uniform(1, 10)`` sizes because they give a largest group of
         two where ``Constant(1)`` gives one, at identical numbers.
       - ``childless`` -- ``test_a_childless_share_takes_the_proof_away``. A
         childless parent is weighed at zero and its rows go to the others.
       - the parent being declared here --
         ``test_a_parent_this_shape_does_not_build_takes_the_proof_away_too``.
       - ``rows <= parent_rows`` --
         ``test_the_boundary_is_where_the_proof_stops_and_the_refusal_starts``,
         and the two-fan-out pair beside it.
    """
    fan_out = cast("FanOut", table.fields[column])
    parent = cast("type[Model]", table.model._meta.get_field(column).related_model)
    parent_rows = rows_of.get(parent)
    return (
        isinstance(fan_out.sizes, Bounded)
        and fan_out.sizes.distinct_values() == 1
        and not fan_out.childless
        and parent_rows is not None
        and table.rows <= parent_rows
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


def _is_drawn_per_row(declared: object) -> bool:
    """Whether this column's value is decided by the row index and nothing else.

    True for a :class:`~django_data_shape.distributions.distribution.Distribution`
    and false for everything else, and it is the distinction the refusal above
    rests on rather than a convenience. A distribution's contract is that it is a
    pure function of the row index and of a draw derived from the field name and
    that same index; a
    :class:`~django_data_shape.derivations.derivation.Derivation` is the one kind
    of declaration that reads something else, which is exactly what makes it the
    only thing able to arrange values around a group.

    ``isinstance`` against ``Distribution`` is not available -- it is a plain
    ``Protocol``, and making it runtime-checkable would test for a ``value``
    attribute on the instance, which is the check that gets an ``Enum`` member
    wrong. So the same structural test :func:`_is_declaration` makes is used, and
    a derivation is subtracted from it: both carry ``value``, and only a
    derivation carries ``scope`` and ``sources`` beside it.
    """
    return _is_declaration(declared) and not isinstance(declared, Derivation)


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
