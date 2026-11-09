"""Refusing a declaration the schema's own constraints could not hold."""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from typing import Any, cast

from django.db.models import (
    NOT_PROVIDED,
    CharField,
    DateField,
    DateTimeField,
    DecimalField,
    DurationField,
    Field,
    FloatField,
    IntegerField,
    JSONField,
    Model,
    Q,
    SmallIntegerField,
    TextField,
    UniqueConstraint,
    UUIDField,
    Value,
)
from django.db.models.functions import Now

from django_data_shape.declaration.field_names import field_names
from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.declaration.projection import Projection
from django_data_shape.declaration.table import Table
from django_data_shape.derivations.derivation import Derivation
from django_data_shape.derivations.per_parent import PerParent
from django_data_shape.distributions.bounded import Bounded
from django_data_shape.distributions.categorical import Categorical
from django_data_shape.distributions.constant import Constant
from django_data_shape.distributions.distinct import Distinct
from django_data_shape.distributions.sequential import Sequential
from django_data_shape.relations.count_roots import count_roots
from django_data_shape.relations.fan_out import FanOut
from django_data_shape.relations.gives_no_row_two_children import gives_no_row_two_children
from django_data_shape.relations.paired import Paired
from django_data_shape.relations.tree import Tree


class _LeftOut(Constant):
    """What a column the shape leaves out holds: one value, in every row.

    ``COPY`` does not name the column, so the database fills it: a literal or
    ``Now()`` ``db_default`` where it has one, and NULL where it has none. A ``Constant``
    because that is what it amounts to for the arithmetic and for a condition, and
    a class of its own so that a refusal says where the value came from rather
    than naming a declaration the caller never wrote.
    """

    @property
    def holds_null(self) -> bool:
        return self._value is None

    def __repr__(self) -> str:
        if self.holds_null:
            return "<left out, NULL in every row>"
        return f"<left out, its db_default {self._value!r} in every row>"


class _NotNull:
    """What ``__isnull=False`` asks of a row: any value at all, which a NULL is not."""

    def __repr__(self) -> str:
        return "NOT NULL"


_NOT_NULL = _NotNull()


class _Unread:
    """A condition's member this module cannot evaluate: an ``F()`` or another expression.

    It names another column, or something the database computes, so whether a row meets it
    depends on what else the row holds. It is read as possibly met by any value but NULL,
    since a comparison with NULL is never true, and never as one key.
    """

    def __init__(self, expression: object) -> None:
        self.expression = expression

    def __repr__(self) -> str:
        return repr(self.expression)


def check_constraints(tables: tuple[Table | Projection, ...]) -> None:
    """Refuse a shape whose row counts contradict the models' own constraints.

    The third net -- the database itself -- catches every one of these, and
    catches it with a terrible message: a unique index failing at row 700,000 of
    a load that has already run for a minute, naming an index rather than a
    declaration. So the arithmetic is done here instead, from
    ``Model._meta.constraints`` and ``Model._meta.unique_together``, before a
    single row is generated.

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
    :meth:`~django_data_shape.declaration.table.Table._check_satisfiable` declines to
    attempt, and it declines for a good reason: a table alone does not know how
    many companies there are. A whole shape does, which is why this runs here.

    **Enough room is not a way to fill it**, which is the second check and the
    one no arithmetic reaches. An unconditional constraint over two fan-outs --
    the through table of a many-to-many -- passes the pigeonhole comfortably,
    because the product of two parent counts dwarfs the row count, and still
    cannot be built: two partitions of the same rows are computed independently,
    so the pairs they produce are an artefact of the row index and a collision
    is a matter of the seed. It is refused rather than counted. So is a ``Paired`` under a
    constraint that does not name the relation it is paired over, since it keeps its
    partners apart only inside each group of that relation.

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

    **What this does not read, and leaves to the other two nets.** A condition
    that names no set of values for one column -- a negation, two clauses joined
    by AND, a comparison -- is skipped, because reading it would mean this package
    deciding what an arbitrary predicate matches. ``__isnull`` and a ``Value`` are read,
    and an ``F()`` or another expression as the value is read as a member that may be
    met, never as one that is not. A constraint over ``expressions`` rather than
    ``fields`` is skipped for the same reason. A derivation in a conditional
    constraint's group is skipped, because whether its ``compute=`` keeps the rule is not
    readable here: ``Derived("owner", ..., scope="group")`` keeps it and one grouped by
    another relation does not. A column the shape leaves out whose
    ``db_default`` is an expression other than ``Now()`` is skipped too, because nothing
    here can read what it writes: ``gen_random_uuid()`` keeps a constraint, and
    :func:`_held` says why ``Now()`` is the one that can be read.
    In every one of those cases the declaration is allowed through and the
    post-load check and the database are what catch it.

    **What this cannot count, and refuses anyway.** A distribution that cannot
    enumerate itself -- anything not
    :class:`~django_data_shape.distributions.categorical.Categorical` -- under a
    condition is read as able to land inside it, so ``Uniform(1, 9)`` under
    ``Q(status="accepted")`` is refused although it would load: unknown is read as a
    yes. A fan-out over a parent this shape does not build, or one carrying a null
    share while each NULL is its own group, leaves only the capacity unknown. The
    arrangement refusals never needed it, so they stand, and what drops out is the
    arithmetic in the message. A key under a condition is read the same way where
    ``parents=`` does not settle it, since this runs before any parent's keys are read, so
    ``Q(owner=99)`` over five people is refused although it would load, over a fan-out and
    over draws alone. So is ``Q(owner=1)`` over a ``Uniform(1, 10**9)``, since a wide draw
    is not ``Distinct``. See :func:`_check_a_relation_in_a_condition`.

    A :class:`~django_data_shape.declaration.projection.Projection` is skipped entirely. Its
    columns are copied along a join rather than drawn from distributions, so
    there is no declared share to compare a capacity against.
    """
    rows_of: dict[type[Model], int] = {
        table.model: table.rows for table in tables if isinstance(table, Table)
    }
    for table in tables:
        if isinstance(table, Projection):
            continue
        for name, fields, condition in _uniquenesses(table.model):
            _check_one(table, name, fields, condition, rows_of)
        _check_a_fan_out_on_a_unique_column(table, rows_of)
        _check_a_unique_column_left_to_one_value(table)


def _uniquenesses(model: type[Model]) -> Iterable[tuple[str, tuple[str, ...], Q | None]]:
    """Every multi-column uniqueness the model states, whichever way it states it.

    ``UniqueConstraint(fields=...)`` lands in ``_meta.constraints``.
    ``Meta.unique_together`` does not: Django keeps it on
    ``_meta.unique_together`` and builds the same unique index from it, so a loop
    over the constraints alone accepted a through table spelled the older way
    and the load died inside ``COPY`` on the index it never read. Both are read
    here and checked as one thing, because to the database they are one thing.

    A ``unique_together`` entry has no name of its own -- Django names its index
    with a hash -- so the tuple is what a refusal names instead, written the way
    the caller wrote it in ``Meta``.

    The columns come back by field name, whichever way they were written.
    ``fields=["parent_id", "name"]`` is one index with ``fields=["parent", "name"]``,
    and a shape is keyed on names, so an attname here read as a column the shape
    left out and the load died inside ``COPY``.
    """
    for constraint in model._meta.constraints:
        if isinstance(constraint, UniqueConstraint) and constraint.fields:
            yield constraint.name, field_names(model, constraint.fields), constraint.condition
    for together in model._meta.unique_together:
        yield f"unique_together {tuple(together)!r}", field_names(model, together), None


def _check_one(
    table: Table,
    name: str,
    fields: tuple[str, ...],
    condition: Q | None,
    rows_of: dict[type[Model], int],
) -> None:
    # Above the pigeonhole, because a column read here counts as one value there,
    # and a NULL that keeps rows out of the index must not count as anything.
    if _a_column_keeps_every_row_apart(table, name, fields, rows_of):
        return
    capacity = _capacity(table, name, fields, rows_of)
    if condition is None:
        # Order is the whole of what keeps the arithmetic reachable. All three
        # can apply to one declaration, and the pigeonhole is the most useful of
        # them: "this shape does not fit at all" is a different instruction from
        # "it fits and nothing arranges it". Moving either arrangement check
        # above it would answer the wrong question first and, under a 100%
        # branch gate with no pragma, would take the 0.8.0 message down with it.
        _check_unconditional(table, name, fields, capacity)
        # Below the pigeonhole, which may answer first. The arrangement checks below
        # each read a fan-out alone as some other shape, and none of them as this one.
        # test_two_fan_outs_under_one_uniqueness_are_refused_by_name holds the length,
        # and test_a_tree_alone_under_a_uniqueness_is_refused_as_a_unique_column_is the
        # isinstance.
        if len(fields) == 1 and isinstance(table.fields.get(fields[0]), FanOut):
            _check_a_fan_out_alone(table, name, fields[0], rows_of)
            return
    # A collision takes two rows, and the rest of this is about arranging them. Held by
    # db_defaults_on_one_row in test_each_exemption_is_accepted_and_loads.
    if table.rows < 2 or _a_fan_out_gives_no_group_two_rows(table, name, fields, rows_of):
        return
    if condition is None:
        _check_a_tree_beside_a_draw(table, name, fields)
        _check_independent_fan_outs(table, name, fields, rows_of)
        _check_a_pairing_whose_nulls_are_one_group(table, name, fields)
        # After the one above, which refuses the pairings whose relation is here and whose
        # groups this constraint does not see, so every one left beside its relation keeps it.
        _check_a_pairing_without_its_relation(table, name, fields, rows_of)
        # After the two-fan-out check, so a tree beside two fan-outs keeps the
        # refusal that can offer a pairing; Paired is a remedy for those two
        # and the tree is then beside a pair that is already distinct.
        _check_a_tree_beside_a_partition(table, name, fields, rows_of)
        _check_a_fan_out_beside_a_draw(table, name, fields, capacity, rows_of)
        _check_drawn_columns_only(table, name, fields, capacity)
        return
    decoded = _condition(condition)
    if decoded is None:
        return
    column, values = decoded
    # By name through the concrete fields rather than field_names, because a condition
    # may say Q(pk=...), which is no field. The attname is held by
    # test_a_condition_may_name_a_left_out_key_by_its_attname. None is a column nothing
    # here can read, and the guard is held by
    # test_a_condition_nothing_can_read_is_left_alone: without it, None reads as the
    # value NULL and Q(token=None) is refused.
    named = _attname_to_name(table.model).get(column, column)
    declared = _held(table, named)
    if declared is None:
        return
    if _a_pairing_keeps_every_pair_apart(table, name, fields):
        return
    # Ahead of the gate and the two below it. A relation takes no PerParent, so a constraint
    # with no partition to offer one over loses nothing here, and what this refusal offers
    # builds over draws alone: test_the_refusal_offers_only_what_builds loads each. The
    # return is for the reader and no test can hold it, since _produces reads every relation
    # as False, so the two below would pass one through anyway.
    if isinstance(declared, (FanOut, Paired, Tree)):
        _check_a_relation_in_a_condition(table, name, fields, named, values, declared, rows_of)
        return
    grouping = [table.fields.get(field) for field in fields]
    # The two below offer a PerParent or read a family, and there is neither here.
    if not any(isinstance(one, (FanOut, Tree)) for one in grouping):
        _check_conditional_over_draws(table, name, fields, column, values, declared)
        return
    # A fan-out first, whatever else is grouped beside it: one row per company
    # is enough to keep (parent, company), so its PerParent stays the remedy
    # even with a tree in the constraint.
    if any(isinstance(one, FanOut) for one in grouping):
        _check_conditional(table, name, fields, capacity, column, values, declared)
    else:
        _check_conditional_over_a_tree(table, name, fields, column, values, declared)


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

    **Unless one of the two partitions has no group of two**, a proof rather than a
    probability that :func:`_a_fan_out_gives_no_group_two_rows` has answered before
    this runs: such a fan-out never repeats a parent key, so no two rows share that
    column and the pair is distinct on that half alone. A third column can
    keep the constraint as well, a ``Distinct`` one or a flat tree, and
    :func:`_a_column_keeps_every_row_apart` has answered for those before this runs.

    That is why this is otherwise a refusal and not a wider capacity
    calculation: no arithmetic over the two marginals decides it, because the
    failure is that nothing enumerates the combinations at all. Deduplicated edges are a
    different algorithm rather than a bigger loop. **That algorithm now exists**
    as :class:`~django_data_shape.relations.paired.Paired`, which is the remedy this
    refusal names: one side stays a fan-out and the other takes distinct
    partners inside its groups, so a duplicate pair is impossible rather than
    removed. A statement of your own is still the answer for an edge set that
    has to be a particular one --
    :class:`~django_data_shape.declaration.projection.Projection` with ``columns=`` and
    ``sql=`` fills the table from a select that can deduplicate.
    """
    fanned = sorted(name for name in fields if isinstance(table.fields.get(name), FanOut))
    if len(fanned) < 2:
        return
    where = table.model.__name__
    # The pairing keeps partners apart inside each group of its relation, and a null share
    # counted as one group is a group it never kept: tied_two_fan_outs_paired_null_kept.
    no_null = (
        f" with no null share on {fanned[0]}"
        if _a_null_share_counted_together(table, name, fanned[:1])
        else ""
    )
    raise InvalidShape(
        f"{name} needs every ({', '.join(fields)}) combination distinct, and "
        f"{', '.join(f'{where}.{column}' for column in fanned)} are fan-outs. "
        f"{_a_null_share_counted_together(table, name, fanned)}A fan-out is a "
        "partition of this table's rows over one parent's keys, computed from the row index "
        "alone, so two of them partition the same rows without either seeing the other: which "
        "pairs come out together is an artefact of that index, and two rows sharing a pair is a "
        "matter of the seed. The combinations do fit, which is why the pigeonhole arithmetic "
        "lets this through -- what is missing is anything that enumerates them, and the load "
        "then fails inside COPY at a row number that moves when the seed does. An edge table "
        f"declares one side and pairs the other: {fanned[1]}=Paired({fanned[0]!r}, Zipf()){no_null} "
        f"takes distinct partners inside each {fanned[0]} group, so no pair can repeat and the edge "
        "count is still exactly the row count. An edge set that has to be a particular one is "
        f"filled by a statement instead -- Projection({where}, columns=(...), sql=...)."
    )


def _check_a_pairing_whose_nulls_are_one_group(
    table: Table, name: str, fields: tuple[str, ...]
) -> None:
    """A ``Paired`` beside its own relation, whose null share this constraint counts as one.

    ``Paired`` keeps partners distinct inside each group of its relation, which is the
    promise that keeps the pair. A row whose relation is NULL had a group when its partner
    was chosen and shows none afterwards, so under ``nulls_distinct=False`` those rows are
    one group holding partners that were never kept apart. Measured on
    ``testapp_tied_two_fan_outs_paired_null_kept``: no loads in twenty, and twenty without
    the null share. Each NULL apart keeps those rows out of the index:
    ``apart_two_fan_outs_paired_null_kept`` loaded twenty times.
    """
    for column in fields:
        paired = table.fields.get(column)
        if not (
            isinstance(paired, Paired)
            and paired.relation in fields
            and not _its_relation_keeps_the_groups(table, name, paired)
        ):
            continue
        where = table.model.__name__
        raise InvalidShape(
            f"{name} needs every ({', '.join(fields)}) combination distinct, and {where}.{column} "
            f"is paired inside each group of {where}.{paired.relation}. "
            f"{_a_null_share_counted_together(table, name, [paired.relation])}Partners are kept "
            "apart inside a group, and the rows of that null share had one when theirs were "
            "chosen, so two of them sharing a partner is a matter of the seed, and the load then "
            "fails inside COPY at a row number that moves when the seed does. The pairing keeps "
            f"the constraint with no null share on {paired.relation}. A table whose rows have to "
            "be a particular set is filled by a Projection with columns= and sql=."
        )


def _check_a_pairing_without_its_relation(
    table: Table, name: str, fields: tuple[str, ...], rows_of: dict[type[Model], int]
) -> None:
    """A ``Paired`` under a uniqueness that does not name the relation it is paired over.

    ``Paired`` keeps partners distinct inside each group of its relation and nowhere else,
    so one partner falls to rows of different groups, and a constraint that does not name
    the relation sees those rows as one. Measured on ``testapp_paired_alone_without_its_relation``,
    two hundred rows over fifty companies and three hundred people: no loads in twenty,
    every one an IntegrityError, and none beside a drawn column or with a null share
    counted as one group either.

    What keeps it has answered before this runs: a relation with one parent, whose one
    group is every row, in :func:`_a_column_keeps_every_row_apart`, and any other column
    that keeps every row apart. A relation over parents this shape does not build is
    refused, since a proof resting on a count nothing here can read is no proof. A second
    ``Paired`` beside its own relation keeps the pair it is in, and so the constraint:
    ``cc_paired_beside_a_kept_pair`` loads twenty times in twenty.

    The offers each loaded twenty times in twenty where they are made. A fan-out that
    gives each partner one row, flat or capped, where the partners can hold every row:
    over fewer partners than rows each loaded none. The column left out, while each NULL is
    its own group: none where they are one. A ``Distinct`` column beside it, where one is
    drawn, which over 150 partners loaded nineteen times, the twentieth the build refusing
    a partner count short of the busiest group, a refusal that stays the build's. And a
    statement. Adding the relation to the constraint keeps it too, and is not offered,
    since that changes the rule rather than the shape.

    .. warning::

       **Conjuncts the coverage gate cannot see.**
       ``test_a_pairing_beside_a_kept_pair_is_accepted_and_loads`` holds the exemption,
       ``test_a_pairing_without_its_relation_is_offered_only_what_builds`` each offer's
       condition and the bound on the partners from both sides, and
       ``test_a_fan_out_of_one_row_per_partner_is_offered_where_the_partners_are_unknown``
       the unknown count.
    """
    if _a_pairing_keeps_every_pair_apart(table, name, fields):
        return
    for column in fields:
        paired = table.fields.get(column)
        # A Paired beside its relation keeps its groups by now, so the exemption answered:
        # the one above refused each that does not.
        if not isinstance(paired, Paired):
            continue
        where = table.model.__name__
        offers: list[str] = []
        partners = _parents_available(table, column, paired, rows_of)
        if partners is None or table.rows <= partners:
            offers.append(
                f"{column}=FanOut(Constant(1)), or a FanOut with max_children=1, which gives each "
                f"partner one row at most over at least as many partners as {where} has rows"
            )
        if _each_null_is_its_own_group(table, name) and _may_be_left_out(table, column):
            offers.append(f"{column} left out of the shape, which holds NULL so no row is in it")
        if any(_is_drawn_per_row(_held(table, field)) for field in fields):
            offers.append(
                "a column of the constraint that is distinct in every row, which says so with "
                "Distinct -- Sequential does"
            )
        raise InvalidShape(
            f"{name} needs every ({', '.join(fields)}) combination distinct, and {where}.{column} "
            f"is paired inside each group of {where}.{paired.relation}, which the constraint "
            "does not name. Partners are kept apart inside a group and never across groups, so "
            f"one partner falls to rows of different {paired.relation} groups, and two of them "
            "sharing a combination is a matter of the seed: the load then fails inside COPY at a "
            "row number that moves when the seed does. What keeps it: "
            f"{'; '.join([*offers, 'a Projection with columns= and sql='])}."
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

    **Two exemptions, both proofs, and both answered before this runs.** A ``Distinct``
    column, in :func:`_a_column_keeps_every_row_apart`, and a partition that gives no
    parent two rows, in :func:`_a_fan_out_gives_no_group_two_rows`. Neither is "this
    usually works": the measured shapes that merely usually work sat at ten and eleven
    times out of twenty, and they are the case this refusal is for.

    **What this does not decide.** A column filled by a
    :class:`~django_data_shape.derivations.derivation.Derivation` is left alone,
    and that is the point of the exemption rather than a gap in it: a derivation
    reads something other than its own row index, so it is the one kind of
    declaration that *can* be arranged around a group --
    ``Derived("company", compute=..., scope="group")`` receives this row's
    position inside its parent's children and can hand back a value per
    position. Whether a particular ``compute`` actually does is not readable
    here, and refusing on a callable this package cannot read would be the
    refusal that is wrong. :func:`_held` says what a column the shape leaves out
    holds: one value, which is read here as a draw that never moves, or a NULL
    in every row, which is its own group and has answered before this runs.

    A fan-out with nothing beside it never reaches this: :func:`_check_one` hands it to
    :func:`_check_a_fan_out_alone`, the unique column it amounts to.
    """
    fanned = [column for column in fields if isinstance(table.fields.get(column), FanOut)]
    others = [column for column in fields if column not in fanned]
    declared = [_held(table, column) for column in others]
    # One condition rather than three, because it is one question: is this the
    # shape of declaration this refusal is about? Two fan-outs are the check
    # above and answer with a different remedy, none at all is a constraint no
    # partition groups, and a derivation is the exemption the docstring gives.
    if len(fanned) != 1 or not all(_is_drawn_per_row(one) for one in declared):
        return
    where = table.model.__name__
    drawn = ", ".join(f"{where}.{column}={_held(table, column)!r}" for column in others)
    # Table refuses scope='group' over any null share, since its rows belong to no group.
    no_null = (
        f" over {fanned[0]} with no null share"
        if cast("FanOut", table.fields[fanned[0]]).null
        else ""
    )
    room = (
        f"The {capacity} combinations do fit the {table.rows} rows"
        if capacity is not None
        else "There may be room for every row"
    )
    raise InvalidShape(
        f"{name} needs every ({', '.join(fields)}) combination distinct, and {where}."
        f"{fanned[0]} is a fan-out beside {drawn}. "
        f"{_a_null_share_counted_together(table, name, fanned)}A fan-out is a partition of this "
        "table's rows "
        "over one parent's keys, computed from the row index alone, and a distribution is drawn "
        "from that same index and nothing else -- so neither column can see what the other put "
        "in the row, and nothing enumerates the combinations inside a group. Whether two of one "
        f"parent's rows draw the same value is a matter of the seed. {room}, which is why the "
        "pigeonhole arithmetic lets this through, and the load then fails inside COPY at a row "
        "number that moves when the seed does. A value that varies with the group is derived "
        f"from it rather than drawn beside it: Derived({fanned[0]!r}, compute=..., scope='group')"
        f"{no_null} receives this row's position among its parent's children and how many there "
        "are. A "
        "column that is distinct in every row keeps the constraint on its own, and says so with "
        "Distinct -- Sequential does."
    )


def _check_a_tree_beside_a_draw(table: Table, name: str, fields: tuple[str, ...]) -> None:
    """A tree and drawn columns under one uniqueness: siblings drawing alike.

    ``Folder(parent, name)`` unique on the pair, the rule a file system keeps
    among siblings, with ``parent=Tree(...)`` and ``name`` drawn from a
    distribution. A tree gives a row several children whenever its sizes say
    so, and a drawn name is a pure function of its own row index, so it cannot
    see which siblings it has. Two of them sharing a name is a matter of the
    seed, and the load fails inside ``COPY``.

    This is :func:`_check_a_fan_out_beside_a_draw` for a tree, and it is a check
    of its own because **neither its exemption nor its remedy carries over**.
    :func:`_cannot_collide` reads a fan-out's cap, childless share and parents,
    and a tree has none of them. Its own proof is
    :func:`_a_tree_gives_no_row_two_children`. The roots are no exemption, since
    each root's NULL is its own group but the siblings below it still collide.
    The fan-out's remedy, a ``Derived(..., scope='group')``, is refused over a
    tree by :class:`~django_data_shape.declaration.table.Table`, because a
    tree's families are not ranges of rows. So the message names only what a
    tree accepts: a column distinct in every row, or a statement of your own.

    The exemption that remains here is the one every arrangement check carries, and
    :func:`_is_drawn_per_row` is what applies it: a derivation is not drawn per row,
    so it leaves the constraint to the other two nets, and so does a column
    :func:`_held` cannot read. A column that is ``Distinct``, a NULL in every row and
    a tree that gives no row two children have answered before this runs, in
    :func:`_a_column_keeps_every_row_apart`. A tree beside a fan-out or a second tree
    is :func:`_check_a_tree_beside_a_partition`, which runs after this and
    relies on ``len(trees) != 1`` here: loosened to ``not trees``, two trees
    come back as one column or as one tree beside a draw, and
    ``test_two_trees_under_one_uniqueness_are_refused_as_two_trees`` and
    ``test_two_trees_beside_a_draw_are_refused_as_two_trees`` are what fail.

    **A tree alone under a constraint is decided here too**, because it is the
    unique column :class:`~django_data_shape.declaration.table.Table` decides,
    spelled as a constraint: ``unique=True`` is read off the field there, and a
    ``UniqueConstraint(fields=["parent"])`` never reaches that check. Both accept a
    tree that gives no row two children, as a unique key to another table accepts
    ``FanOut(max_children=1)``, so the spellings keep saying one thing:
    ``test_a_flat_tree_alone_under_a_uniqueness_is_accepted``. The rest are refused,
    and the refusal offers the flat tree only where the proof would hold for it.
    """
    trees = [column for column in fields if isinstance(table.fields.get(column), Tree)]
    others = [column for column in fields if column not in trees]
    declared = [_held(table, column) for column in others]
    # One question, as in the fan-out check: is every other column a per-row
    # draw? A tree beside anything that is not a draw is a shape this refusal
    # is not about. A tree with nothing beside it passes this vacuously, and is
    # then the unique column it amounts to.
    if len(trees) != 1 or not all(_is_drawn_per_row(one) for one in declared):
        return
    where = table.model.__name__
    if not others:
        raise InvalidShape(_a_tree_alone(table, name, where, trees[0]))
    drawn = ", ".join(f"{where}.{column}={_held(table, column)!r}" for column in others)
    raise InvalidShape(
        f"{name} needs every ({', '.join(fields)}) combination distinct, and {where}."
        f"{trees[0]} is a Tree beside {drawn}. A tree gives a row several children whenever "
        "its sizes say so, and a distribution is drawn from the row index alone, so no child "
        "can see what its siblings drew: two of them sharing a value is a matter of the seed, "
        "and the load then fails inside COPY at a row number that moves when the seed does. "
        "Nothing is decided per family of a tree, since a family is not a range of rows. A "
        "column that is distinct in every row keeps the constraint on its own and says so with "
        f"Distinct -- Sequential does. So does {_a_flat_tree(table, name)}. A table whose rows "
        "have to be a particular set is "
        "filled by a Projection with columns= and sql=."
    )


def _a_tree_alone(table: Table, name: str, where: str, column: str) -> str:
    """Why a tree alone under this constraint is refused, and what would fill the column.

    Two different answers, because the second has less to offer. Under
    ``nulls_distinct=False`` a tree of many roots collides however flat it is, and
    leaving the column out loads the same NULL in every row, so neither may be
    named. What is left is one root, which is one NULL.
    """
    tree = cast("Tree", table.fields[column])
    if not _each_null_is_its_own_group(table, name):
        return (
            f"{name} permits one row per {where}.{column} and counts every NULL as one value "
            "under nulls_distinct=False. A tree's roots are the rows whose parent is NULL, so "
            "two roots collide as two children of one row do, and a column left out of the "
            f"shape holds that one NULL in every row. What fills it is {_a_flat_tree(table, name)}, or, "
            "where the rows have to be a particular set, a Projection with columns= and sql=."
        )
    return (
        f"{name} permits one row per {where}.{column}, so each row may be the parent of one row "
        f"at most, and {tree!r} spreads children over parents by weight, which gives some parents "
        "several. A tree whose sizes hold one value gives no row two children and fills it: "
        f"{column}=Tree(roots={tree.roots!r}, sizes=Constant(1)). Or leave the column out and "
        "fill it after the build."
    )


def _check_a_tree_beside_a_partition(
    table: Table, name: str, fields: tuple[str, ...], rows_of: dict[type[Model], int]
) -> None:
    """A tree beside a fan-out or a second tree: two choices, neither seeing the other.

    ``Branch(parent, company)`` unique on the pair, or ``Edge(left, right)`` with
    both keys into its own table. This is :func:`_check_independent_fan_outs`
    with a tree in it: each column is computed from the row index and streams
    keyed on its own name, so which pairs come out together is an artefact of
    that index. Measured over twenty seeds on tables named ``testapp_branch`` and
    ``testapp_edge``, a tree beside ``FanOut(Zipf())`` loaded no times at two
    hundred rows over fifty companies, and ten times at fifty rows over a
    thousand, and another count under another name, since the draws are keyed on
    them. Two trees loaded no times at two hundred rows.

    **A tree's remedies are narrower than a fan-out's.** ``Paired`` takes distinct
    partners inside a fan-out's groups and ``Derived(..., scope='group')`` numbers
    them, and :class:`~django_data_shape.declaration.table.Table` refuses both
    over a tree, because a tree's families are not ranges of rows. So what is left
    is a column distinct in every row, the key left out, or a statement.

    **The exemptions are the ones the arrangement checks carry.** A pair is
    distinct as soon as either half is, so a ``Distinct`` column keeps the
    constraint, and so does a tree
    :func:`_a_tree_gives_no_row_two_children` proves, and
    :func:`_a_column_keeps_every_row_apart` has answered for both before this runs.
    So does a fan-out :func:`_no_group_of_two` proves gives no parent two rows, which
    :func:`_a_fan_out_gives_no_group_two_rows` has answered before this runs. A column
    that is not drawn per row, which is a derivation, a column :func:`_held` cannot read
    or a ``Paired``, is left to the other two nets.

    No tree here is not a separate case: two fan-outs that get this far have been
    refused by :func:`_check_independent_fan_outs` already, so they never do.
    """
    trees = [column for column in fields if isinstance(table.fields.get(column), Tree)]
    fanned = [column for column in fields if isinstance(table.fields.get(column), FanOut)]
    if len(trees) + len(fanned) < 2:
        return
    others = [column for column in fields if column not in trees and column not in fanned]
    declared = [_held(table, column) for column in others]
    if not all(_is_drawn_per_row(one) for one in declared):
        return
    where = table.model.__name__
    named = [
        *(f"{where}.{column} is a Tree" for column in trees),
        *(f"{where}.{column} is a fan-out" for column in fanned),
        *(f"{where}.{column}={_held(table, column)!r} is drawn per row" for column in others),
    ]
    apart = _each_null_is_its_own_group(table, name)
    left_out = (
        f"Leaving {trees[0]} out of the shape loads NULL, and each NULL in a unique index is its "
        "own group, so it can be filled after the build. "
        if apart
        else ""
    )
    # A cap of one needs a parent for every row, and leaves a null share one group of its own
    # where NULLs are one value: tied_tree_beside_a_fan_out_cap_null_kept loaded 1 of 20, and
    # tree_beside_a_fan_out_cap_rows_over_parents is refused by the build.
    room = [column for column in fanned if _room_for_a_cap(table, column, rows_of)]
    no_null = " and no null share" if _a_null_share_counted_together(table, name, room) else ""
    capped = (
        "A fan-out that gives no parent two rows keeps the constraint on its own and says so with "
        f"max_children=1{no_null}. "
        if room
        else ""
    )
    raise InvalidShape(
        f"{name} needs every ({', '.join(fields)}) combination distinct, and {', '.join(named)}. "
        f"{_a_null_share_counted_together(table, name, fanned)}"
        "Each of those fills its own column from the row index alone and none can see what "
        "another put in the row, so which combinations come out together is an artefact of that "
        "index and two rows sharing one is a matter of the seed. The load then fails inside COPY "
        "at a row number that moves when the seed does. Nothing is decided per family of a tree, "
        f"since a family is not a range of rows. {capped}A column of the constraint that is "
        f"distinct in every row keeps it too and says so with Distinct -- Sequential does, and so "
        f"does {_a_flat_tree(table, name)}. "
        f"{left_out}A table whose rows have to be a particular set is filled by a Projection with "
        "columns= and sql=."
    )


def _each_null_is_its_own_group(table: Table, name: str | None) -> bool:
    """Whether this constraint counts each NULL apart, which is PostgreSQL's default.

    ``nulls_distinct=False`` makes them one value. Read with ``getattr`` because
    the attribute arrived in Django 5.0. A ``unique_together`` matches no
    constraint's name here, and its index always keeps the default, and so does a
    field's own ``unique=True``, which is None.

    Everything that rests on the answer: :func:`_a_column_keeps_every_row_apart`,
    :func:`_a_tree_gives_no_row_two_children`, :func:`_no_group_of_two`,
    :func:`_can_land_inside`, :func:`_its_relation_keeps_the_groups` and :func:`_capacity`. ``Distinct`` needs no reading, since a column of different
    values holds one NULL at most, so it keeps the rows apart either way.
    """
    return all(
        getattr(constraint, "nulls_distinct", None) is not False
        for constraint in table.model._meta.constraints
        if constraint.name == name
    )


def _a_column_keeps_every_row_apart(
    table: Table, name: str, fields: tuple[str, ...], rows_of: dict[type[Model], int]
) -> bool:
    """Whether one column settles this constraint, whatever the others do.

    A combination is distinct as soon as one of its columns is, so four proofs
    answer for every check at once, and one spelling cannot read a proof another
    ignores. A ``Distinct`` column, each of whose rows holds a value no other row
    holds. A NULL in every row, while each NULL is its own group, which keeps every
    row out of the index. A tree in which no row has two children. And a ``Paired``
    whose relation has one parent: its partition is one group, partners are distinct
    inside a group, so every row holds a partner no other row holds.
    ``paired_key_alone_over_one_parent`` and ``paired_key_alone_over_one_declared_parent``
    load twenty times in twenty and hold it, each spelling of the one parent.

    It holds under a condition as well, since a group of one row has nothing to
    count, so a ``PerParent`` is not consulted and may say what it likes.

    .. warning::

       **Conjuncts the coverage gate cannot see.**
       ``test_a_distinct_column_does_not_excuse_one_that_does_not_move`` holds
       ``is_distinct_per_row``. ``holds_null`` is held by
       ``test_a_db_default_is_one_value_in_every_row``, whose literal default keeps no
       row apart. The NULL reading is held by
       ``test_nulls_counted_together_are_one_value_where_the_checks_read_them``.
    """
    if _a_tree_gives_no_row_two_children(table, name, fields):
        return True
    held = [_held(table, column) for column in fields]
    if any(isinstance(one, Distinct) and one.is_distinct_per_row() for one in held):
        return True
    if any(isinstance(one, Paired) and _one_group(table, one, rows_of) for one in held):
        return True
    return _each_null_is_its_own_group(table, name) and any(
        isinstance(one, Constant) and one.value(0, 0.0) is None for one in held
    )


def _attname_to_name(model: type[Model]) -> dict[str, str]:
    """The field name behind each ``_id`` column, which a condition may say either way."""
    return {field.attname: field.name for field in model._meta.concrete_fields}


def _held(table: Table, column: str) -> object:
    """What this column holds: its declaration, what the load leaves there, or None.

    A column the shape leaves out is not named in ``COPY``, so the database fills
    it, and that used to be read as a NULL whatever the column was. A literal
    ``db_default`` is the same value in every row, measured at no loads in twenty
    beside a tree and beside a fan-out. A column with none holds NULL, since
    :class:`~django_data_shape.declaration.table.Table` refuses one left out that
    cannot, and a ``Value(None)`` default is the same NULL.

    ``Now()`` is read as a literal is, and it is the one expression that is. Django
    compiles it to ``STATEMENT_TIMESTAMP()`` on PostgreSQL, and all of a table's rows
    arrive in one ``COPY``, so every row holds one instant, which
    ``test_what_an_expression_db_default_writes_across_one_copy`` reads off the table:
    no loads in twenty beside a tree and beside a fan-out, and twenty beside a proof
    about the other column.
    The portable route inserts by chunks, where the instants may differ, and a shape
    is checked before any backend is chosen, so it is refused there as well.

    None is what nothing here can read, and it reads as not drawn per row, which
    leaves the constraint to the other two nets. Any other ``db_default`` that is an
    expression, since ``gen_random_uuid()`` keeps a constraint and a call this package
    has not read may do either. A ``GeneratedField``, which the database computes per
    row. A key, which this package fills itself. And ``pk``, which a condition may say
    and no field is called.

    .. warning::

       **Conjuncts the coverage gate cannot see**, each of which loads when it is
       kept and is refused when it is not, in
       ``test_each_exemption_is_accepted_and_loads``: ``primary_key`` by
       ``db_default_on_the_key_this_package_fills``, ``generated`` by
       ``generated_column_counted_together``, ``resolve_expression`` by
       ``expression_db_default_new_in_every_row``, ``field is None`` by
       ``condition_on_the_key``. ``test_a_db_default_is_one_value_in_every_row`` holds
       the ``Now`` reading, on its two ``now_`` parameters, and the ``Value`` unwrap, on
       ``written_as_a_value``.
    """
    if column in table.fields:
        return table.fields[column]
    field = next(
        (one for one in table.model._meta.concrete_fields if one.name == column),
        None,
    )
    if field is None or field.primary_key or getattr(field, "generated", False):
        return None
    default = getattr(field, "db_default", NOT_PROVIDED)
    if default is NOT_PROVIDED:
        return _LeftOut(None)
    # Django 5.0 wraps a literal in a Value and later versions keep it bare, and a
    # caller can write the Value themselves, so both spell one value.
    literal = default.value if isinstance(default, Value) else default
    if hasattr(literal, "resolve_expression") and not isinstance(literal, Now):
        return None
    return _LeftOut(literal)


def _one_group(table: Table, paired: Paired, rows_of: dict[type[Model], int]) -> bool:
    """Whether a pairing's relation is known to spread its rows over one parent."""
    relation = cast("FanOut", table.fields[paired.relation])
    return _parents_available(table, paired.relation, relation, rows_of) == 1


def _a_flat_tree(table: Table, name: str) -> str:
    """The tree a refusal may offer to keep this constraint on its own, in words.

    One that gives no row two children, and under ``nulls_distinct=False`` one with a
    single root as well, since the roots' NULLs are then one group. The three tree
    refusals share this one reading: ``test_roots_counted_as_one_group_collide_beside_a_fan_out``
    fails if the root clause is never offered, and ``test_a_tree_beside_a_fan_out_is_refused``
    if it is always offered.
    **1/(rows + 1), not 1/rows**: :func:`count_roots` reads a share through its decimal
    spelling, and ``repr(1/11)`` is a hair above the fraction, so ``1/rows`` is two roots
    at rows 11, 13, 15, 22, 26 and on. ``test_the_one_root_a_refusal_offers_is_one_root_at_any_row_count``
    and ``test_the_tree_the_finding_names_under_nulls_counted_as_one_is_accepted`` hold it.
    """
    if _each_null_is_its_own_group(table, name):
        return "a tree with sizes that hold one value, such as Constant(1), which give no row two children"
    return (
        f"a tree with one root, roots at most 1/{table.rows + 1}, whose sizes hold one value, "
        "since one root is one NULL"
    )


def _a_null_share_counted_together(table: Table, name: str | None, fanned: Iterable[str]) -> str:
    """The sentence a refusal owes when the NULLs of a fan-out are what collide."""
    shares = [
        column
        for column in fanned
        if cast("FanOut", table.fields[column]).null
        and not _each_null_is_its_own_group(table, name)
    ]
    if not shares:
        return ""
    where = table.model.__name__
    return (
        f"{name} counts every NULL as one value under nulls_distinct=False, so the rows a null "
        f"share leaves in {', '.join(f'{where}.{column}' for column in shares)} are one group "
        "there, however few each parent is given. "
    )


def _a_tree_gives_no_row_two_children(table: Table, name: str, fields: tuple[str, ...]) -> bool:
    """Whether a tree among these columns provably keeps the constraint on its own.

    **A proof rather than a probability**, the line :func:`_cannot_collide` draws
    for a fan-out. A combination holding a key no other row holds is distinct
    whatever the other columns do, and a root's NULL is its own group, so a tree
    in which no row has two children cannot collide. That last is
    :func:`~django_data_shape.relations.gives_no_row_two_children.gives_no_row_two_children`.

    **Or where there is one root.** Under ``nulls_distinct=False`` the roots are one
    group and collide among themselves, so a second root is what breaks the proof,
    and with a single root the one NULL is as distinct as every key. A flat tree of
    many roots stays refused and so does one root over siblings.
    The ``test_roots_counted_as_one_group_*`` tests hold the NULL reading, and
    ``db_default_beside_a_flat_tree`` in ``test_each_exemption_is_accepted_and_loads``
    holds ``apart``. That test holds the root's bound from below, on
    ``one_root_alone``, and ``test_one_root_is_not_enough_and_nor_is_a_flat_tree``
    from above.
    """
    apart = _each_null_is_its_own_group(table, name)
    return any(
        (apart or count_roots(tree, table.rows) <= 1)
        and gives_no_row_two_children(tree, table.rows)
        for tree in (table.fields.get(column) for column in fields)
        if isinstance(tree, Tree)
    )


def _a_fan_out_gives_no_group_two_rows(
    table: Table, name: str, fields: tuple[str, ...], rows_of: dict[type[Model], int]
) -> bool:
    """Whether one fan-out here gives no group two rows, which settles every check below.

    **One is enough**: a combination is distinct as soon as one column is. Twenty rows
    over twenty companies partitioned flat, beside a Zipf over five people, loaded twenty
    times in twenty, and so did the same proof under a condition, which it leaves nothing
    to count. Not in :func:`_a_column_keeps_every_row_apart`, which runs above the
    pigeonhole, where a cap of one over too few parents is refused with its arithmetic.

    ``test_a_cap_of_one_keeps_a_pair_unique_beside_a_second_fan_out`` holds ``any``,
    ``test_every_constraint_this_cannot_read_is_left_to_the_other_two_nets`` the
    ``isinstance``, and
    ``test_a_cap_of_one_under_a_condition_is_no_proof_where_its_nulls_are_one_group``
    :func:`_no_group_of_two` in place of :func:`_cannot_collide`.
    """
    return any(
        isinstance(table.fields.get(column), FanOut)
        and _no_group_of_two(table, name, column, rows_of)
        for column in fields
    )


def _no_group_of_two(
    table: Table, name: str | None, column: str, rows_of: dict[type[Model], int]
) -> bool:
    """Whether this fan-out provably gives no group two rows, under this constraint.

    :func:`_cannot_collide` answers for the parents. A null share is one more group
    under ``nulls_distinct=False``, and a large one, so the proof stops there: a cap
    of one with a null share loaded no times in twenty.

    .. warning::

       **Conjuncts the coverage gate cannot see.**
       ``test_nulls_counted_together_are_one_value_where_the_checks_read_them`` holds
       the whole of the left side. ``tied_left_out_column_beside_a_cap_of_one`` in
       ``test_each_exemption_is_accepted_and_loads`` holds ``not fan_out.null``, and
       ``test_a_null_share_keeps_the_cap_while_each_null_is_its_own_group`` holds the
       NULL reading. ``test_a_skew_beside_a_fan_out_under_a_partial_constraint_is_refused``
       holds the right side.
    """
    fan_out = cast("FanOut", table.fields[column])
    return (not fan_out.null or _each_null_is_its_own_group(table, name)) and _cannot_collide(
        table, column, rows_of
    )


def _check_a_fan_out_alone(
    table: Table, name: str | None, column: str, rows_of: dict[type[Model], int]
) -> None:
    """A fan-out alone under a uniqueness, which permits one row per parent.

    One path for both spellings of that index, because they used to answer one shape
    differently. ``name`` is the constraint, or None for the field's own ``unique=True``,
    which :func:`_check_a_fan_out_on_a_unique_column` reads. A collision takes two rows,
    and a fan-out :func:`_no_group_of_two` proves gives no parent two of them cannot put
    two on one parent. ``test_one_row_under_a_fan_out_alone_is_accepted_either_way``
    holds ``rows``, which ``unique=True`` used to skip, and
    ``test_a_fan_out_that_cannot_collide_keeps_a_one_to_one`` the proof.
    """
    if table.rows < 2 or _no_group_of_two(table, name, column, rows_of):
        return
    fan_out = cast("FanOut", table.fields[column])
    where = f"{table.model.__name__}.{column}"
    rule = (
        f"{where} is unique, so it holds one row at most per parent"
        if name is None
        else f"{name} permits one row per {where}"
    )
    tied = _a_null_share_counted_together(table, name, [column])
    # The sentence above says the null share's rows are one group, so an offer that keeps the
    # share is refused again: test_a_fan_out_alone_offers_what_loads_once_the_null_share_is_gone.
    no_null = " and no null share" if tied else ""
    capped = (
        f"max_children=1{no_null}, which caps every parent at one row whatever the sizes weigh, "
        "or by "
        if _room_for_a_cap(table, column, rows_of)
        else ""
    )
    raise InvalidShape(
        f"{rule}, and {fan_out!r} is a partition that exists to give some parents more than one, "
        "so the load fails inside COPY at a row number that moves when the seed does. "
        f"{tied}A one-to-one is filled by {capped}FanOut(Constant(1)) with no childless "
        f"share{no_null} over at least as many parents as this table has rows."
    )


def _room_for_a_cap(table: Table, column: str, rows_of: dict[type[Model], int]) -> bool:
    """Whether a cap of one on this fan-out could hold every row, so a refusal may offer it.

    The build counts every row against the parents a cap leaves room for, a null share's
    included, and refuses the partition before ``COPY`` where they do not fit. A parent this
    shape does not build has however many rows it has, so the offer stands there.
    """
    parent_rows = _parents_available(table, column, cast("FanOut", table.fields[column]), rows_of)
    return parent_rows is None or table.rows <= parent_rows


def _check_a_fan_out_on_a_unique_column(table: Table, rows_of: dict[type[Model], int]) -> None:
    """A partition over a column that permits one row per parent.

    ``unique=True`` writes no entry in ``_meta.constraints`` -- it is a unique
    *index* -- so the loop above never sees it, and the single-column check on
    :class:`~django_data_shape.declaration.table.Table` steps over a fan-out on purpose:
    whether a partition gives any parent two rows depends on how many parents
    there are, which one table does not know and a whole shape does. So a
    ``OneToOneField`` filled by ``FanOut(Zipf())`` fell between the two, and
    **it does not merely usually fail -- measured, it never loads**, zero runs
    in twenty at fifty rows over a hundred parents. A skewed partition exists to
    give some parent several children, and a one-to-one is the column that
    permits none. :func:`_check_a_fan_out_alone` decides it, as it does the same
    index written as a constraint. ``test_a_fan_out_on_a_one_to_one_is_refused`` holds
    the ``isinstance``, and ``unique`` is held by every shape with a skewed fan-out on a
    column that is not unique, ``test_a_projection_under_its_ceiling_builds`` among them.
    """
    for column, declared in table.fields.items():
        field = cast("Field[Any, Any]", table.model._meta.get_field(column))
        if isinstance(declared, FanOut) and field.unique:
            _check_a_fan_out_alone(table, None, column, rows_of)


def _what_makes_a_unique_column_new(field: Field[Any, Any], rows: int) -> str:
    """The remedy for a unique left-out column, named for the field and loaded at several seeds.

    **A distribution, because a db_default new in every row has one true instance.** A
    ``DateTimeField`` under ``Now()`` has none: ``clock_timestamp()`` loaded 0 of 3, and
    the reading of a left-out column in :func:`_held` takes it as one value anyway. A
    ``UUIDField`` has ``gen_random_uuid()``, and no ``Distinct`` distribution to name.
    ``DateTimeField`` is a ``DateField``, so it is asked first.
    ``test_the_distribution_a_unique_left_out_column_is_offered_loads`` builds the three
    distributions, and ``test_a_unique_uuid_left_out_is_offered_the_db_default_that_is_new_in_every_row``
    holds the fourth.

    **Sequential(1, 1) only where :func:`_counts_to` says it builds**, and a Projection
    alone anywhere else, since no other distribution here is ``Distinct``.
    """
    if isinstance(field, UUIDField):
        return (
            "Give it a db_default the database makes new in every row, such as "
            "Func(function='gen_random_uuid', output_field=UUIDField())."
        )
    if isinstance(field, DateTimeField):
        start = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        offered = Sequential(start, datetime.timedelta(seconds=1))
    elif isinstance(field, DateField):
        offered = Sequential(datetime.date(2020, 1, 1), datetime.timedelta(days=1))
    elif _counts_to(field, rows):
        offered = Sequential(1, 1)
    else:
        return (
            f"No Distinct distribution here fits a {type(field).__name__} of {rows} rows, so "
            "fill it with a Projection with columns= and sql=."
        )
    return (
        f"Declare the column with a distribution that is Distinct, such as {field.name}={offered!r} "
        "(Sequential implements Distinct)."
    )


def _counts_to(field: Field[Any, Any], rows: int) -> bool:
    """Whether ``Sequential(1, 1)`` writes 1 to ``rows`` into this column and the load takes it.

    Measured at two hundred rows, twenty seeds each. Integers, floats, JSON and a duration
    load every time. Text loads where its max_length holds the digits of ``rows`` and fails
    with a value too long one short of it, and a decimal where its digits before the point
    do, failing with a numeric overflow. A time, an IP address, binary and a boolean load
    never, each a TypeError or a ValidationError, and so does any field not named here.
    A small integer, positive or not, holds 32767 at most, so it loads up to that many rows
    and fails one row past it with a value out of range; an integer's own ceiling is two
    billion rows away. ``test_the_fallback_is_offered_where_it_builds`` and
    ``test_the_fallback_is_withheld_where_it_cannot_build`` hold each type and every bound,
    and ``test_the_fallback_is_offered_up_to_the_last_value_a_small_integer_holds`` the
    small integer's from below.
    """
    digits = len(str(rows))
    if isinstance(field, SmallIntegerField):
        return rows <= 2**15 - 1
    if isinstance(field, DecimalField):
        return field.max_digits - field.decimal_places >= digits
    if isinstance(field, (CharField, TextField)):
        return field.max_length is None or field.max_length >= digits
    return isinstance(field, (IntegerField, FloatField, JSONField, DurationField))


def _check_a_unique_column_left_to_one_value(table: Table) -> None:
    """A ``unique=True`` column the shape leaves to a ``db_default`` of one value.

    Its own check for the reason :func:`_check_a_fan_out_on_a_unique_column` is:
    ``unique=True`` writes no entry in ``_meta.constraints``, so the reading of a left-out
    column that :func:`_check_one` does never reaches it. Measured at no loads in twenty,
    a literal and ``Now()`` alike. A column left out with no default holds a NULL per row,
    and each is its own group, and an expression :func:`_held` cannot read stays with the
    other two nets.

    .. warning::

       **Conjuncts the coverage gate cannot see**, each of which loads when it is kept
       and is refused when it is not, in ``test_each_exemption_is_accepted_and_loads``:
       ``rows`` by the two ``on_one_row`` parameters, ``unique`` by
       ``a_column_not_unique_repeats_its_db_default``, ``_LeftOut`` by
       ``unique_column_declared_despite_its_db_default``, ``holds_null`` by
       ``unique_column_left_out_holds_a_null_per_row``.
    """
    if table.rows < 2:
        return
    for field in table.model._meta.concrete_fields:
        held = _held(table, field.name)
        if field.unique and isinstance(held, _LeftOut) and not held.holds_null:
            raise InvalidShape(
                f"{table.model.__name__}.{field.name} is unique and holds {held!r}, so "
                f"{table.rows} rows cannot load. {_what_makes_a_unique_column_new(field, table.rows)}"
            )


def _check_drawn_columns_only(
    table: Table, name: str, fields: tuple[str, ...], capacity: int | None
) -> None:
    """No fan-out anywhere, and the same defect: nothing enumerates the tuple.

    The third instance, and the one that shows **the fan-out was never what was
    special**. ``Coupon(batch, code)`` unique on the pair, both columns drawn
    from a
    :class:`~django_data_shape.distributions.distribution.Distribution` and no
    relation in the table at all. Every mechanism this package has for filling a
    column computes it from the row index alone, so no column can see what
    another put in the row -- and a partition being absent removes a way of
    arranging the pairs rather than the need for one.

    **The measurements are the argument, because "there is room" reads like an
    answer.** Fifty rows over three hundred combinations, six times the room:
    zero loads in twenty. The same fifty rows over ten thousand combinations,
    **two hundred times the room**: eighteen. Room moves the odds and never
    settles them, because a draw is not a permutation -- and a shape that works
    nineteen times in twenty is the one this refusal is for, since the twentieth
    run is somebody else's afternoon.

    The exemption is the one the other two carry: a pair is distinct as soon as
    either half is, so a column that answers
    :class:`~django_data_shape.distributions.distinct.Distinct` keeps the
    constraint on its own, and :func:`_a_column_keeps_every_row_apart` has answered
    for it before this runs. There is deliberately no partition exemption here,
    because there is no partition -- ``_cannot_collide`` has nothing to read.

    Two kinds of column take the refusal away for reasons that are not
    exemptions at all. A
    :class:`~django_data_shape.derivations.derivation.Derivation` reads
    something other than its own row index, which is exactly what lets it be
    arranged across rows, and whether a given ``compute=`` is arranged is not
    readable here. A column the shape leaves out and holds NULL in is its own group
    in a unique index, so those rows cannot collide with anything, and that has
    answered before this runs as well; under ``nulls_distinct=False`` it is one value
    like any other and :func:`_held` reads it so.
    """
    declared = [_held(table, column) for column in fields]
    if any(isinstance(one, FanOut) for one in declared):
        return
    if not all(_is_drawn_per_row(one) for one in declared):
        return
    where = table.model.__name__
    room = (
        f"The {capacity} combinations are more than the {table.rows} rows"
        if capacity is not None
        else "There may be room for every row"
    )
    drawn = ", ".join(f"{where}.{column}={_held(table, column)!r}" for column in fields)
    raise InvalidShape(
        f"{name} needs every ({', '.join(fields)}) combination distinct, and {drawn} are all "
        "drawn per row with nothing partitioning any of them. A distribution is a pure function "
        "of the row index, so no column can see what another put in the row and nothing "
        f"enumerates the combinations. {room}, which is why the pigeonhole arithmetic lets this "
        "through -- and room is not what decides it: fifty rows over two hundred times as many "
        "combinations still collided in two runs out of twenty, because a draw is not a "
        "permutation. A column that is distinct in every row keeps the constraint on its own and "
        "says so with Distinct, which Sequential implements. A table whose rows have to be a "
        "particular set of combinations is filled by a Projection with columns= and sql=."
    )


def _cannot_collide(table: Table, column: str, rows_of: dict[type[Model], int]) -> bool:
    """Whether this fan-out provably gives no parent two rows to draw for.

    The second exemption, and it is a **proof rather than a probability**, which
    is the line this refusal is drawn on. A collision under a constraint like
    this one is always two rows of the *same* group drawing the same value, so a
    partition in which no group holds two rows cannot produce one, whatever the
    other column draws and whatever the seed.

    The first way is a cap of one. ``max_children=1`` bounds the partition
    itself, so no parent holds two rows whatever the sizes weigh, whatever the
    childless share, and wherever the parents come from; a declaration whose
    rows outnumber the parents that can hold them is refused when the partition
    is drawn, naming the arithmetic, before a row reaches ``COPY``.

    Without it, four conditions, and all four are needed. The sizes have to be provably flat
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

       **The coverage gate cannot see these five conditions, and the tests are
       what hold them.** They are one boolean expression, so they are one branch
       arc: drop any single conjunct and the suite still reports 100% line and
       branch, because the surviving expression is still taken both ways. The
       guard against that is one test per condition, and each has been falsified
       against a tree with that condition removed. If you edit this expression,
       edit those with it -- they are, in order:

       - ``max_children == 1`` --
         ``test_a_cap_of_one_is_a_proof_whatever_the_sizes``, beside the
         ``test_a_cap_of_two_is_not`` that keeps the comparison from widening.
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
    parent_rows = _parents_available(table, column, fan_out, rows_of)
    return (fan_out.max_children == 1 and _room_for_a_cap(table, column, rows_of)) or (
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
    values: tuple[object, ...],
    declared: object,
) -> None:
    """One row per group may sit inside the condition. Who is filling it?

    ``values`` is what satisfies the condition -- one member for an equality,
    several for an ``__in``. Nothing below distinguishes the two, because
    nothing below needs to: a row is inside the condition if its value is any
    of them, and the constraint permits one such row per group either way.

    The exemptions have answered before this runs, in :func:`_check_one`: a column
    that keeps every row apart, :func:`_a_column_keeps_every_row_apart`, and a fan-out
    that leaves no group two rows, :func:`_no_group_of_two`.
    """
    where = f"{table.model.__name__}.{column}"
    group = ", ".join(fields)
    matching = _matching(column, values)
    # The first fan-out rather than the first field: a PerParent reads a group
    # from a partition, and Table refuses one over anything else, a tree included.
    fanned = next(field for field in fields if isinstance(table.fields.get(field), FanOut))
    if isinstance(declared, PerParent):
        if declared.relation not in fields:
            raise InvalidShape(
                f"{where} is decided per group of {declared.relation!r}, but {name} groups by "
                f"({group}). A rule kept once per {declared.relation} says nothing about how "
                f"many rows with {matching} a ({group}) ends up with. Group the "
                f"PerParent by one of the constraint's own fan-outs, such as {fanned!r}."
            )
        if _produces_any(declared.special, values) is not False and declared.count > 1:
            raise InvalidShape(
                f"{where} puts {declared.special!r} on {declared.count} rows of every group, and "
                f"{name} permits one. count= is what N-winners-per-contest is for, and a unique "
                "constraint is the case it is not."
            )
        # What decides this is the *rest* of the group, whichever side of the
        # condition the special row falls: one special row inside it is exactly
        # what the constraint permits, and none at all is fewer. A set makes the
        # case reachable that an equality made hard to write -- `last` and
        # `rest` can both be inside the condition, so the declaration that
        # answers the equality form breaks this one. PerParent has already
        # narrowed `rest` to a plain value or a distribution that enumerates
        # itself, so this is decidable rather than merely unproven.
        if _produces_any(declared.rest, values) is False:
            return
    elif _produces_any(declared, values) is False:
        return

    permits = (
        f"at most {capacity} rows with {matching}, one per ({group})"
        if capacity is not None
        else f"at most one row with {matching} per ({group})"
    )
    asks = _asks(table, declared, values)
    raise InvalidShape(
        f"{name} permits {permits}; {where} is filled by {declared!r}, which {asks}. A rule about "
        "a group cannot be kept by a draw made per row, at any weight -- a smaller share only "
        f"moves the collision later into the load. {_what_keeps_a_draw_out(table, column, fanned, values)}"
    )


def _what_keeps_a_draw_out(
    table: Table, column: str, fanned: str, values: tuple[object, ...]
) -> str:
    """The remedy for a draw under a condition, which a ``PerParent`` is where it can name one.

    It names the value it puts on one row of each group, so it needs a member to name. A
    condition that is not a set of values leaves the column holding NULL, which meets none
    of it: ``isnull_false_left_out`` and ``left_out_equal_to_an_expression`` loaded twenty
    times in twenty, and so did ``null_equal_to_an_expression``, declared.
    """
    if all(_is_literal(value) for value in values):
        return (
            f"Declare the column with PerParent({fanned!r}, last={values[0]!r}, rest=...), which "
            "puts it on one row of each group and makes the count derived from the fan-out rather "
            "than chosen beside it. rest= has to sit outside the condition, which is the whole of "
            "what makes the group's other rows invisible to it."
        )
    field = cast("Field[Any, Any]", table.model._meta.get_field(column))
    held_null = (
        f"{column}=Constant(None) holds NULL, which meets none of it, and so does {column} left "
        "out of the shape where nothing else fills it. "
        if field.null
        else ""
    )
    return (
        f"The condition is no set of values a PerParent could put on one row of each group. "
        f"{held_null}A table whose rows have to be a particular set is filled by a Projection "
        "with columns= and sql=."
    )


def _check_conditional_over_a_tree(
    table: Table,
    name: str,
    fields: tuple[str, ...],
    column: str,
    values: tuple[object, ...],
    declared: object,
) -> None:
    """One row per family may sit inside the condition, and nothing can arrange that.

    ``Reply(parent, status)`` with one ``accepted`` reply per parent. The defect
    is :func:`_check_conditional`'s: a per-row draw cannot keep a rule about a
    group, at any weight. Measured over twenty seeds at two hundred rows on a table
    named ``testapp_reply``, a share of 10% loaded three times and 2.5% fourteen,
    and other names give other counts, so a smaller share moves the collision
    rather than removing it.

    **It is a check of its own because that remedy is refused here.**
    ``PerParent`` reads a group from a fan-out's ranges, and a tree's families are
    not ranges of rows, so :class:`~django_data_shape.declaration.table.Table`
    refuses it over a tree. A ``PerParent`` over a fan-out outside this constraint
    is accepted by ``Table`` and keeps a different rule, so it is read by what it
    can put inside the condition and refused if that is anything.

    The exemptions are the ones the arrangement checks carry. A grouping column
    distinct in every row leaves each group one row, so the condition has nothing to
    count, and :func:`_a_column_keeps_every_row_apart` has answered for that before
    this runs. A grouping column that is not drawn per row, a derivation or one
    :func:`_held` cannot read, is left to the other two nets. A derivation filling
    the condition column is left to them too, because whether its ``compute=`` lands
    inside the condition is not readable here; ``PerParent`` is the one derivation
    whose values are.

    .. warning::

       **Two conjuncts the coverage gate cannot see.** The ``PerParent`` line is
       one branch arc: ``test_a_rule_kept_per_a_fan_out_outside_the_constraint_is_refused``
       holds ``special``, and
       ``test_a_rule_kept_per_a_fan_out_whose_rest_lands_inside_the_condition_is_refused``
       holds ``rest``.
    """
    trees = [field for field in fields if isinstance(table.fields.get(field), Tree)]
    others = [field for field in fields if field not in trees]
    grouped = [_held(table, field) for field in others]
    if not all(_is_drawn_per_row(one) for one in grouped):
        return
    if isinstance(declared, PerParent):
        if (
            _produces_any(declared.special, values) is False
            and _produces_any(declared.rest, values) is False
        ):
            return
        asks = f"is decided per {declared.relation}, which is no group of this constraint"
    elif isinstance(declared, Derivation) or _produces_any(declared, values) is False:
        return
    else:
        asks = _asks(table, declared, values)
    where = table.model.__name__
    named = [
        *(f"{where}.{field} is a Tree" for field in trees),
        *(f"{where}.{field}={_held(table, field)!r} is drawn per row" for field in others),
    ]
    raise InvalidShape(
        f"{name} permits one row with {_matching(column, values)} per ({', '.join(fields)}), and "
        f"{', '.join(named)}; {where}.{column}={declared!r} {asks}. Nothing is decided per family "
        "of a tree, since a family is not a range of rows, so no row inside the condition can see "
        "whether a sibling is inside it too. Two of them sharing a group is a matter of the seed "
        "at any weight, and the load then fails inside COPY at a row number that moves when the "
        f"seed does. What a tree accepts: {column} filled so that no row lands inside the "
        "condition; a column of the constraint that is distinct in every row, which says so with "
        f"Distinct -- Sequential does; {_a_flat_tree(table, name)}; or a Projection with columns= "
        "and sql=."
    )


def _check_conditional_over_draws(
    table: Table,
    name: str,
    fields: tuple[str, ...],
    column: str,
    values: tuple[object, ...],
    declared: object,
) -> None:
    """A condition over a constraint no column of which is partitioned.

    ``UniqueConstraint(fields=["label"], condition=Q(state="x"))`` with both drawn. Every row
    draws its label without seeing another, so two rows inside the condition sharing one is
    a matter of the seed at any weight: measured on ``testapp_drawn_condition_over_a_draw``,
    no loads in twenty, and ten at a share of one in a hundred. A ``Paired`` in the group is
    the same defect, since its partners are kept apart only inside its relation's groups, and
    so is a ``PerParent`` over a fan-out outside the constraint, which keeps a different rule.

    **There is no partition here to offer a PerParent over**, which is why this used to be
    skipped. What builds: a column of the constraint that is ``Distinct``, where one of them
    is drawn rather than paired; the condition column left out where its NULL meets none of
    the condition; and a statement. Each loaded twenty times in twenty on the refused
    table's sizes.

    A column of the constraint that is not drawn per row and not a ``Paired`` -- a
    derivation, or one :func:`_held` cannot read -- leaves it to the other two nets, as the
    arrangement checks do: ``Derived("owner", ..., scope="group")`` keeps it, twenty times in
    twenty, and that is not readable here. So is a derivation filling the condition column,
    unless it is a ``PerParent``, whose values are.
    """
    grouped = [_held(table, field) for field in fields]
    if not all(_is_drawn_per_row(one) or isinstance(one, Paired) for one in grouped):
        return
    if isinstance(declared, PerParent):
        if (
            _produces_any(declared.special, values) is False
            and _produces_any(declared.rest, values) is False
        ):
            return
        asks = f"is decided per {declared.relation}, which is no group of this constraint"
    elif isinstance(declared, Derivation) or _produces_any(declared, values) is False:
        return
    else:
        asks = _asks(table, declared, values)
    where = table.model.__name__
    named = [
        f"{where}.{field} is paired per {one.relation}"
        if isinstance(one, Paired)
        else f"{where}.{field}={one!r} is drawn per row"
        for field, one in zip(fields, grouped, strict=True)
    ]
    offers: list[str] = []
    if any(not isinstance(one, Paired) for one in grouped):
        offers.append(
            "a column of the constraint that is distinct in every row, which says so with Distinct "
            "-- Sequential does"
        )
    if _may_be_left_out(table, column) and _produces_any(_LeftOut(None), values) is False:
        offers.append(f"{column} left out of the shape, which holds NULL so no row is inside")
    raise InvalidShape(
        f"{name} permits one row with {_matching(column, values)} per ({', '.join(fields)}), and "
        f"{', '.join(named)}; {where}.{column}={declared!r} {asks}. No column of the constraint is "
        "partitioned, so nothing is decided per group and no row inside the condition can see "
        "whether another in its group is inside too. Two of them sharing a group is a matter of "
        "the seed at any weight, and the load then fails inside COPY at a row number that moves "
        f"when the seed does. What keeps it: {'; '.join([*offers, 'a Projection with columns= and sql='])}."
    )


def _check_a_relation_in_a_condition(
    table: Table,
    name: str,
    fields: tuple[str, ...],
    column: str,
    values: tuple[object, ...],
    declared: FanOut | Paired | Tree,
    rows_of: dict[type[Model], int],
) -> None:
    """The condition names a relation, so what it tests is a key this package chose.

    ``UniqueConstraint(fields=["company"], condition=Q(owner=1))`` with ``owner`` a
    fan-out. The defect is :func:`_check_conditional`'s: which row holds a key comes from
    the row index, so no row inside the condition can see whether another in its group is
    inside too, and two of them sharing a group is a matter of the seed. Size moves the odds
    and settles nothing: ``Q(owner=1)`` loaded twenty times in twenty at two hundred rows
    over three hundred people, and one in twenty over ten.

    **It is a check of its own because that remedy is refused here.** ``PerParent`` is a
    derivation, and :class:`~django_data_shape.declaration.table.Table` refuses anything
    but a ``FanOut``, a ``Paired`` or a ``Tree`` on a relation. So the message offers what
    :func:`_what_keeps_a_relation_out` says would build for this declaration.

    Three ways to be accepted. Nothing can land inside, which is
    :func:`_can_land_inside`. At most one row per group lands inside, which is
    :func:`_keeps_one_row_inside_per_group`. Or a column beside the relation is not drawn
    per row, a derivation or one :func:`_held` cannot read, which leaves it to the other
    two nets, as :func:`_check_conditional_over_a_tree` does. A ``Distinct`` column, a flat
    tree in the constraint and a fan-out that leaves no group two rows have answered before
    this runs, and so has a ``Paired`` beside its own relation.

    **No partition in the constraint changes nothing here.** ``Q(owner=1)`` over a drawn
    ``label`` is the same defect: every row inside draws its label without seeing another,
    and it loaded 0 times in 20. Every remedy below loads there as well.

    **A key the parent does not have is read as able to land inside.** ``Q(owner=99)``
    over five people loads every time and is refused, as ``Uniform(1, 9)`` is under
    ``Q(status="accepted")``: unknown is read as a yes, and this package does not read a
    parent's keys until the build.
    """
    inside = tuple(
        value for value in values if _can_land_inside(table, name, fields, column, declared, value)
    )
    if not inside or _keeps_one_row_inside_per_group(
        table, name, fields, column, inside, declared, rows_of
    ):
        return
    others = [
        field for field in fields if not isinstance(table.fields.get(field), (FanOut, Paired, Tree))
    ]
    if not all(_is_drawn_per_row(_held(table, field)) for field in others):
        return
    where = table.model.__name__
    remedies = _what_keeps_a_relation_out(table, column, inside, declared, others, rows_of)
    raise InvalidShape(
        f"{name} permits one row with {_matching(column, inside)} per ({', '.join(fields)}), and "
        f"{where}.{column}={declared!r} can write that. A relation's keys are spread over the "
        "rows from the row index alone, so no row inside the condition can see whether another "
        "in its group is inside too. Two of them sharing a group is a matter of the seed at any "
        "size, and the load then fails inside COPY at a row number that moves when the seed "
        "does. PerParent is no way out, since a relation takes only a FanOut, a Paired or a "
        f"Tree. What keeps it: {remedies}."
    )


def _can_land_inside(
    table: Table,
    name: str,
    fields: tuple[str, ...],
    column: str,
    declared: FanOut | Paired | Tree,
    value: object,
) -> bool:
    """Whether a row this relation fills can hold ``value`` and count under the constraint.

    NULL first. A row whose NULL is in a column of the constraint is out of the index while
    each NULL is its own group, so it counts for nothing. Otherwise a fan-out writes one
    only with a null share, a tree always, since its first row is a root, and a ``Paired``
    never, since it asks for a partner per row and the null share of its relation thins
    only that relation.

    A key is inside unless ``parents=`` leaves it out, which a tree does not have. Compared
    as text because a key is written in a condition and in ``parents=`` by hand, and
    ``"1"`` and ``1`` are one key to the database.

    .. warning::

       **Conjuncts the coverage gate cannot see.** ``null_share`` in
       ``test_a_relation_that_can_land_inside_the_condition_is_refused`` holds
       ``column in fields``, and ``null_share_grouped_together`` the NULL reading.
    """
    if value is None:
        if column in fields and _each_null_is_its_own_group(table, name):
            return False
        return bool(declared.null) if isinstance(declared, FanOut) else isinstance(declared, Tree)
    # A key, which parents= may leave out. Anything else is every key or perhaps any.
    if not _is_literal(value):
        return True
    parents = None if isinstance(declared, Tree) else declared.parents
    return parents is None or str(value) in {str(key) for key in parents}


def _keeps_one_row_inside_per_group(
    table: Table,
    name: str,
    fields: tuple[str, ...],
    column: str,
    inside: tuple[object, ...],
    declared: FanOut | Paired | Tree,
    rows_of: dict[type[Model], int],
) -> bool:
    """Whether the one value left inside can give a group two rows. A proof, or False.

    **One value, because two are two rows.** A cap of one leaves each key one row, so a set
    of two keys is two rows that may share a group: ``Q(owner__in=[1, 2])`` under
    ``max_children=1`` loaded ten times in twenty over five companies.

    A key on a fan-out that gives no parent two rows, which is :func:`_cannot_collide`
    rather than :func:`_no_group_of_two`, because a null share is no matter here: the
    condition reads a key, and those rows hold none. A tree that gives no row two children
    is the same proof, and NULL on a tree is its roots, so one root is one row. A key on a
    ``Paired`` grouped by its own relation has one row per group, since partners are
    distinct inside a group, unless NULLs of that relation are one group.

    **The same pairing from its other end.** ``Q(company=1)`` over ``(person)``, with
    ``person=Paired("company", ...)``, puts many rows inside, all of one company, so every
    one holds a different person. It loaded twenty times in twenty at four rows of every
    company over four people. A key holds none of the relation's NULLs, so a null share is
    no matter, and the rows that hold NULL are the ones refused, since they had no group to
    keep their partners apart in.

    .. warning::

       **Conjuncts the coverage gate cannot see.** ``set_under_a_cap`` holds the one value,
       ``null_share_under_a_cap`` holds ``is not None``, and
       ``cap_of_one_with_nulls_together`` keeps :func:`_no_group_of_two` from standing in.
       ``roots_of_a_flat_tree`` and ``key_over_one_root`` hold the two tree readings apart.
       ``paired_under_another_fan_out`` holds the relation being in the constraint.
       ``relation_set_over_its_pairing`` holds the one value for the pairing from its other
       end, ``relation_null_over_its_pairing`` holds ``is not None`` for it, and
       ``another_key_over_a_pairing`` holds its ``relation == column``. No test holds its
       ``isinstance`` apart from that: a derivation also has a ``relation``, and one in the
       constraint is left to the other two nets whichever way this answers.
    """
    if len(inside) != 1 or not _is_literal(inside[0]):
        return False
    if isinstance(declared, Tree):
        if inside[0] is None:
            return count_roots(declared, table.rows) <= 1
        return gives_no_row_two_children(declared, table.rows)
    if isinstance(declared, FanOut):
        return inside[0] is not None and (
            _cannot_collide(table, column, rows_of)
            or any(
                isinstance(paired, Paired) and paired.relation == column
                for paired in (table.fields.get(field) for field in fields)
            )
        )
    return declared.relation in fields and _its_relation_keeps_the_groups(table, name, declared)


def _a_pairing_keeps_every_pair_apart(table: Table, name: str, fields: tuple[str, ...]) -> bool:
    """Whether a ``Paired`` and its own relation are both in this constraint.

    Partners are distinct inside each group of the relation, so no pair repeats and the
    constraint holds whatever the condition says, which is the promise ``Paired`` makes.
    Each loaded twenty times in twenty, and that is what the condition's own reading
    cannot see, since ``Q(company=1)`` over the pair puts many rows inside.

    ``paired_key_over_its_relation`` is the one column's version, in
    :func:`_keeps_one_row_inside_per_group`. ``paired_beside_another_fan_out`` holds the
    relation being here, and ``paired_pair_null_relation_together`` the NULL reading: a
    null share loads thirteen times in twenty there.
    """
    return any(
        isinstance(paired, Paired)
        and paired.relation in fields
        and _its_relation_keeps_the_groups(table, name, paired)
        for paired in (table.fields.get(field) for field in fields)
    )


def _its_relation_keeps_the_groups(table: Table, name: str, paired: Paired) -> bool:
    """Whether this constraint sees the groups the pairing kept its partners distinct in.

    A row with a NULL relation had a group when the partners were chosen and shows none
    afterwards. While each NULL is its own group that keeps it out of the index, and under
    ``nulls_distinct=False`` it joins one group holding partners that were never kept apart.
    """
    relation = cast("FanOut", table.fields[paired.relation])
    return not relation.null or _each_null_is_its_own_group(table, name)


def _what_keeps_a_relation_out(
    table: Table,
    column: str,
    inside: tuple[object, ...],
    declared: FanOut | Paired | Tree,
    others: list[str],
    rows_of: dict[type[Model], int],
) -> str:
    """Only the remedies that would build for this declaration, last the one that always does.

    ``test_the_refusal_offers_only_what_builds`` loads each of them. A narrowed ``Paired``
    is not offered: it is accepted, and whether it builds depends on the busiest group
    against the partners left, which nothing reads here.
    """
    offers: list[str] = []
    if isinstance(declared, FanOut):
        offers.extend(_what_keeps_a_fan_out_out(table, column, inside, declared, rows_of))
    elif isinstance(declared, Tree) and len(inside) == 1 and _is_literal(inside[0]):
        offers.append(
            f"{column}=Tree(roots=1/{table.rows + 1}, sizes=...), since one root is one row with NULL"
            if inside[0] is None
            else "a tree with sizes that hold one value, such as Constant(1), which give that row "
            "one child at most"
        )
    if None not in inside and _may_be_left_out(table, column):
        offers.append(f"{column} left out of the shape, which holds NULL so no row is inside")
    if others:
        offers.append(
            "a column of the constraint that is distinct in every row, which says so with Distinct "
            "-- Sequential does"
        )
    return "; ".join([*offers, "a Projection with columns= and sql="])


def _what_keeps_a_fan_out_out(
    table: Table,
    column: str,
    inside: tuple[object, ...],
    declared: FanOut,
    rows_of: dict[type[Model], int],
) -> list[str]:
    """Narrowing the keys away and dropping the null share, or a cap that leaves one row.

    Narrowing needs a parent to narrow to and the cap needs one for every row, so each is
    offered only where the parent's row count says so. ``key_over_the_only_parent`` and
    ``key_over_unknown_parents`` are what fail otherwise.
    """
    # Every key, or any: no parents= leaves those out, and no cap leaves them one row.
    if not all(_is_literal(value) for value in inside):
        return []
    keys = [value for value in inside if value is not None]
    parent_rows = _parents_available(table, column, declared, rows_of)
    offers: list[str] = []
    if not keys:
        offers.append("no null share")
    elif parent_rows is None or parent_rows > len(keys):
        narrowed = "parents= naming only keys outside the condition"
        offers.append(narrowed if len(keys) == len(inside) else f"{narrowed} and no null share")
    if len(inside) == 1 and keys and parent_rows is not None and table.rows <= parent_rows:
        offers.append("max_children=1, which gives that key one row at most")
    return offers


def _may_be_left_out(table: Table, column: str) -> bool:
    """Whether leaving this column out loads a NULL, with nothing else needing it.

    ``key_on_a_required_column`` holds ``null``, ``key_on_a_defaulted_column`` the default,
    and ``key_read_by_owner`` and ``key_read_by_label`` the two readers, each of which
    ``Table`` refuses without the relation it reads.
    """
    field = cast("Field[Any, Any]", table.model._meta.get_field(column))
    read = any(
        (isinstance(one, Paired) and one.relation == column)
        or (isinstance(one, Derivation) and column in one.sources)
        for one in table.fields.values()
    )
    return not read and field.null and getattr(field, "db_default", NOT_PROVIDED) is NOT_PROVIDED


def _asks(table: Table, declared: object, values: tuple[object, ...]) -> str:
    """The declaration's own arithmetic, quoted back at it when it has any.

    The shares of **every** member are totalled, because any of them puts the
    row inside the condition. Quoting one member's share against a set would
    understate the ask, and understating it in a refusal is how a caller
    concludes the refusal is wrong.
    """
    if not all(_is_literal(value) for value in values):
        return "may meet the condition in any row, which nothing here can rule out"
    if isinstance(declared, Constant):
        # Not through shares(), which keys the value and raises on a dict: a JSONField left
        # out under a dict db_default. test_a_dict_db_default_is_compared_to_a_condition_not_hashed
        # holds it, by a condition the column meets.
        return f"asks for {table.rows if declared.value(0, 0.0) in values else 0} of them"
    if isinstance(declared, Categorical):
        shares = declared.shares()
        share = sum(weight for value, weight in shares.items() if value in values)
        return f"asks for {round(table.rows * share)} of them"
    if len(values) == 1:
        return f"draws {values[0]!r} independently per row"
    return f"draws one of ({', '.join(repr(value) for value in values)}) independently per row"


def _capacity(
    table: Table, name: str, fields: tuple[str, ...], rows_of: dict[type[Model], int]
) -> int | None:
    """How many distinct combinations of ``fields`` this declaration can build.

    None where any one of them cannot be counted, because a capacity that
    guessed low would refuse a shape that builds perfectly well -- and a refusal
    that is sometimes wrong is worse than the load failure it replaces.

    ``test_a_null_share_counted_together_is_one_more_parent`` holds ``and apart`` and
    the ``+ 1`` below, which the coverage gate cannot see.
    """
    apart = _each_null_is_its_own_group(table, name)
    total = 1
    for column in fields:
        declared = _held(table, column)
        if isinstance(declared, FanOut):
            # A null share is not a smaller capacity, it is an unbounded one:
            # PostgreSQL treats every NULL in a unique index as distinct, so
            # those rows are exempt from the constraint entirely. Under
            # nulls_distinct=False they are one more parent instead.
            if declared.null and apart:
                return None
            parent_rows = _parents_available(table, column, declared, rows_of)
            if parent_rows is None:
                return None
            total *= parent_rows + (1 if declared.null else 0)
        elif isinstance(declared, Bounded):
            total *= declared.distinct_values()
        else:
            return None
    return total


def _parents_available(
    table: Table, column: str, fan_out: FanOut | Paired, rows_of: dict[type[Model], int]
) -> int | None:
    """How many parents this fan-out can actually point at, or None if unknown.

    A ``Paired`` is read the same way, for the partners it can take.

    ``parents=`` is the answer where it is given, and it is a **better** answer
    than the parent's row count rather than a substitute for it: it is known
    without the parent being declared in this shape at all, and it is what the
    partition will really spread over. Reading the table's rows instead would
    overstate the room by the ratio of the two -- fifty companies declared and
    one pinned -- and an overstated capacity is a pigeonhole that fails to refuse
    a shape which cannot fit.

    Without it, the parent has to be declared here: a parent loaded by the caller
    has however many rows it has, and this runs before anything is built.
    """
    if fan_out.parents is not None:
        return len(fan_out.parents)
    parent = cast("type[Model]", table.model._meta.get_field(column).related_model)
    return rows_of.get(parent)


def _produces_any(declared: object, values: tuple[object, ...]) -> bool | None:
    """Whether this declaration can put **any** of these in a row.

    The three-valued answer of :func:`_produces`, lifted over a set the same way
    the constraint reads it. One provable yes is a yes; every member provably
    absent is the only no, because a single unreadable member leaves the row
    able to land inside the condition. Unknown stays unknown and is read as a
    yes by the caller, which is the direction a refusal must never get wrong.
    """
    return _any_of(_produces(declared, value) for value in values)


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

    A relation never does: comparing its declaration to a key is false for every key,
    so :func:`_check_a_relation_in_a_condition` answers for it instead.
    """
    if isinstance(declared, Constant):
        # Compared, not hashed: shares() keys the value, and a dict db_default is no key.
        return _meets(declared.value(0, 0.0), value)
    if isinstance(declared, Categorical):
        return _any_of(_meets(candidate, value) for candidate in declared.shares())
    return None if _is_declaration(declared) else _meets(declared, value)


def _meets(held: object, member: object) -> bool | None:
    """Whether a row holding ``held`` meets one member of a condition: yes, no, or unknown.

    A member :func:`_members` could not read is met by no NULL, since a comparison with NULL
    is never true, and by anything else perhaps. ``_NOT_NULL`` is met by anything else.
    """
    if isinstance(member, _Unread):
        return None if held is not None else False
    if member is _NOT_NULL:
        return held is not None
    return bool(held == member)


def _any_of(answers: Iterable[bool | None]) -> bool | None:
    """One provable yes is a yes, every no is a no, and anything else is unknown."""
    seen = list(answers)
    if any(answer is True for answer in seen):
        return True
    return False if all(answer is False for answer in seen) else None


def _is_literal(member: object) -> bool:
    """Whether a condition's member is one value, which a key or a share can be compared to."""
    return not isinstance(member, (_NotNull, _Unread))


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


def _condition(condition: Q) -> tuple[str, tuple[object, ...]] | None:
    """The column a condition tests and the values that satisfy it, or None.

    Three spellings of one thing, and they are read as one thing:
    ``Q(column=value)``, ``Q(column__in=[...])``, and an OR of equalities over a
    single column -- which is what ``__in`` compiles to and what an ORM gives no
    reason to prefer either way. An equality comes back as a **set of one**,
    because it is one and nothing downstream cares how many members it has:
    what decides the refusal is whether a row can land inside the condition, and
    membership answers that for every spelling.

    **This has been widened twice, and the second time is the lesson.** Reading
    only the bare equality left the likelier spelling unchecked -- "one open
    review per project" is several statuses far more often than one -- so
    ``__in`` was added. A consumer then pointed out that *their* constraint was
    an OR-tree, and that a fix aimed at the ``__in`` return specifically would
    not reach it. It did not. The shape to check for is *"is this decidable
    from values the caller wrote down"*, not *"is this one of the spellings seen
    so far"*, and widening it twice is what makes that worth writing down.

    Everything else is still declined, and the line has not moved: a negation, a
    comparison like ``__gt``, an ``__in`` over a queryset, clauses joined by AND,
    or an OR whose branches name **different columns** -- which describes no set
    for any one column and would mean deciding what an arbitrary predicate
    matches. That is the database's job. A set of values the caller wrote out is
    not that.
    """
    if condition.negated:
        return None
    if condition.connector == Q.OR:
        return _union(_decode(child) for child in condition.children)
    # AND, which is what a lone clause also arrives as. More than one means two
    # separate predicates, and their conjunction is not a set of values.
    return _decode(condition.children[0]) if len(condition.children) == 1 else None


def _decode(child: object) -> tuple[str, tuple[object, ...]] | None:
    """One branch of a condition: a nested ``Q``, or a ``(lookup, value)`` pair.

    Django hands ``Q(Q(a) | Q(b))`` over as a single child that is itself a
    ``Q``, which is a different object graph from the same expression written
    bare -- so the recursion is what makes the two spellings agree rather than
    an extra case being handled.
    """
    if isinstance(child, Q):
        return _condition(child)
    if not isinstance(child, tuple) or len(child) != 2:
        return None
    lookup, value = child
    column, _, suffix = str(lookup).partition("__")
    # isnull asks for a NULL or for anything else, which is how Q(column=None) already reads.
    # Skipping it read every such condition as never met. Django refuses a value that is not
    # a bool before any index is built.
    if suffix == "isnull":
        return column, (None,) if value else (_NOT_NULL,)
    if suffix in ("", "exact"):
        return column, _members((value,))
    if suffix != "in" or not isinstance(value, (list, tuple, set, frozenset)):
        return None
    # A set has no order of its own, and this tuple reaches the caller's screen
    # in a refusal message. Ordering it by repr keeps that message the same
    # between runs, which a test asserting on it has to be able to rely on.
    return column, _members(
        tuple(value) if isinstance(value, (list, tuple)) else tuple(sorted(value, key=repr))
    )


def _members(written: tuple[object, ...]) -> tuple[object, ...]:
    """The values a condition names, read as what the database compares.

    A ``Value`` is the literal it wraps, and ``Value(None)`` compiles to ``= NULL``, which no
    row meets, so it names nothing. Any other expression is an :class:`_Unread` member.
    """
    members: list[object] = []
    for one in written:
        if isinstance(one, Value):
            if one.value is not None:
                members.append(one.value)
        elif hasattr(one, "resolve_expression"):
            members.append(_Unread(one))
        else:
            members.append(one)
    return tuple(members)


def _union(
    branches: Iterable[tuple[str, tuple[object, ...]] | None],
) -> tuple[str, tuple[object, ...]] | None:
    """Every branch's values, if every branch decodes and names one column.

    A **set**, not a bag: a value named twice is counted once, or the share
    arithmetic downstream would total it twice and quote a number the
    declaration never asked for. Order is the order it was written in, for the
    same reason the set spelling is sorted -- the tuple ends up in a message.

    An empty set needs no special case and deliberately does not get one: it
    matches no row, and ``_produces_any`` answers "provably never" for it
    already, because every member being absent is vacuously true of no members.
    """
    values: dict[object, None] = {}
    column: str | None = None
    for branch in branches:
        if branch is None:
            return None
        found, members = branch
        if column is not None and found != column:
            return None
        column = found
        values.update(dict.fromkeys(members))
    return None if column is None else (column, tuple(values))


def _matching(column: str, values: tuple[object, ...]) -> str:
    """The condition written the way the caller wrote it, for the message."""
    if values == (_NOT_NULL,):
        return f"{column} not NULL"
    if len(values) == 1:
        return f"{column}={values[0]!r}"
    return f"{column} in ({', '.join(repr(value) for value in values)})"
