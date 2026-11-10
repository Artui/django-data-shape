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

    The database fills it: the ``db_default`` where there is one, else NULL."""

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

    It is read as possibly met by any value but NULL, since the row decides it."""

    def __init__(self, expression: object) -> None:
        self.expression = expression

    def __repr__(self) -> str:
        return repr(self.expression)


def check_constraints(tables: tuple[Table | Projection, ...]) -> None:
    """Refuse a shape whose row counts contradict the models' own constraints."""
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
    """Every multi-column uniqueness the model states, whichever way it states it."""
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
    # Order is load-bearing: each check answers a different question, and the first one wins.
    if _a_column_keeps_every_row_apart(table, name, fields, rows_of):
        return
    capacity = _capacity(table, name, fields, rows_of)
    if condition is None:
        _check_unconditional(table, name, fields, capacity)
        if len(fields) == 1 and isinstance(table.fields.get(fields[0]), FanOut):
            _check_a_fan_out_alone(table, name, fields[0], rows_of)
            return
    if table.rows < 2 or _a_fan_out_gives_no_group_two_rows(table, name, fields, rows_of):
        return
    if condition is None:
        _check_a_tree_beside_a_draw(table, name, fields)
        _check_independent_fan_outs(table, name, fields, rows_of)
        _check_a_pairing_whose_nulls_are_one_group(table, name, fields)
        _check_a_pairing_without_its_relation(table, name, fields, rows_of)
        _check_a_tree_beside_a_partition(table, name, fields, rows_of)
        _check_a_fan_out_beside_a_draw(table, name, fields, capacity, rows_of)
        _check_drawn_columns_only(table, name, fields, capacity)
        return
    decoded = _condition(condition)
    if decoded is None:
        return
    column, values = decoded
    named = _attname_to_name(table.model).get(column, column)
    declared = _held(table, named)
    if declared is None:
        return
    if _a_pairing_keeps_every_pair_apart(table, name, fields):
        return
    if isinstance(declared, (FanOut, Paired, Tree)):
        _check_a_relation_in_a_condition(table, name, fields, named, values, declared, rows_of)
        return
    grouping = [table.fields.get(field) for field in fields]
    if not any(isinstance(one, (FanOut, Tree)) for one in grouping):
        _check_conditional_over_draws(table, name, fields, column, values, declared)
        return
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
    """Two fan-outs over one constraint: enough room, and nothing to arrange it."""
    fanned = sorted(name for name in fields if isinstance(table.fields.get(name), FanOut))
    if len(fanned) < 2:
        return
    where = table.model.__name__
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
    """A ``Paired`` beside its own relation, whose null share this constraint counts as one."""
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
    """A ``Paired`` under a uniqueness that does not name the relation it is paired over."""
    if _a_pairing_keeps_every_pair_apart(table, name, fields):
        return
    for column in fields:
        paired = table.fields.get(column)
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
        if any(
            _is_drawn_per_row(_held(table, field)) and not _is_a_relation(table, field)
            for field in fields
        ):
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
    """One fan-out and one drawn column: the same defect, one column over."""
    fanned = [column for column in fields if isinstance(table.fields.get(column), FanOut)]
    others = [column for column in fields if column not in fanned]
    declared = [_held(table, column) for column in others]
    if len(fanned) != 1 or not all(_is_drawn_per_row(one) for one in declared):
        return
    where = table.model.__name__
    drawn = ", ".join(f"{where}.{column}={_held(table, column)!r}" for column in others)
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
    if any(not _is_a_relation(table, column) for column in others):
        remedy = (
            "A value that varies with the group is derived from it rather than drawn beside it: "
            f"Derived({fanned[0]!r}, compute=..., scope='group'){no_null} receives this row's "
            "position among its parent's children and how many there are. A column that is "
            "distinct in every row keeps the constraint on its own, and says so with Distinct -- "
            "Sequential does."
        )
    else:
        remedy = (
            "Every other column of the constraint is a key, which takes a FanOut or a Paired and "
            "neither a derivation nor a distribution, so nothing declared beside the fan-out "
            "arranges it. A table whose rows have to be a particular set is filled by a "
            "Projection with columns= and sql=."
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
        f"number that moves when the seed does. {remedy}"
    )


def _check_a_tree_beside_a_draw(table: Table, name: str, fields: tuple[str, ...]) -> None:
    """A tree and drawn columns under one uniqueness: siblings drawing alike."""
    trees = [column for column in fields if isinstance(table.fields.get(column), Tree)]
    others = [column for column in fields if column not in trees]
    declared = [_held(table, column) for column in others]
    if len(trees) != 1 or not all(_is_drawn_per_row(one) for one in declared):
        return
    where = table.model.__name__
    if not others:
        raise InvalidShape(_a_tree_alone(table, name, where, trees[0]))
    drawn = ", ".join(f"{where}.{column}={_held(table, column)!r}" for column in others)
    if any(not _is_a_relation(table, column) for column in others):
        keeps = (
            "A column that is distinct in every row keeps the constraint on its own and says so "
            "with Distinct -- Sequential does. So does "
        )
    else:
        keeps = "What keeps the constraint on its own is "
    raise InvalidShape(
        f"{name} needs every ({', '.join(fields)}) combination distinct, and {where}."
        f"{trees[0]} is a Tree beside {drawn}. A tree gives a row several children whenever "
        "its sizes say so, and a distribution is drawn from the row index alone, so no child "
        "can see what its siblings drew: two of them sharing a value is a matter of the seed, "
        "and the load then fails inside COPY at a row number that moves when the seed does. "
        f"Nothing is decided per family of a tree, since a family is not a range of rows. {keeps}"
        f"{_a_flat_tree(table, name)}. A table whose rows have to be a particular set is "
        "filled by a Projection with columns= and sql=."
    )


def _a_tree_alone(table: Table, name: str, where: str, column: str) -> str:
    """Why a tree alone under this constraint is refused, and what would fill the column."""
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
    """A tree beside a fan-out or a second tree: two choices, neither seeing the other."""
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
    room = [column for column in fanned if _room_for_a_cap(table, column, rows_of)]
    no_null = " and no null share" if _a_null_share_counted_together(table, name, room) else ""
    capped = (
        "A fan-out that gives no parent two rows keeps the constraint on its own and says so with "
        f"max_children=1{no_null}. "
        if room
        else ""
    )
    if any(not _is_a_relation(table, column) for column in others):
        keeps = (
            "A column of the constraint that is distinct in every row keeps it too and says so "
            "with Distinct -- Sequential does, and so does "
        )
    else:
        keeps = "So does " if room else "What keeps it is "
    raise InvalidShape(
        f"{name} needs every ({', '.join(fields)}) combination distinct, and {', '.join(named)}. "
        f"{_a_null_share_counted_together(table, name, fanned)}"
        "Each of those fills its own column from the row index alone and none can see what "
        "another put in the row, so which combinations come out together is an artefact of that "
        "index and two rows sharing one is a matter of the seed. The load then fails inside COPY "
        "at a row number that moves when the seed does. Nothing is decided per family of a tree, "
        f"since a family is not a range of rows. {capped}{keeps}{_a_flat_tree(table, name)}. "
        f"{left_out}A table whose rows have to be a particular set is filled by a Projection with "
        "columns= and sql=."
    )


def _each_null_is_its_own_group(table: Table, name: str | None) -> bool:
    """Whether this constraint counts each NULL apart, which is PostgreSQL's default."""
    return all(
        getattr(constraint, "nulls_distinct", None) is not False
        for constraint in table.model._meta.constraints
        if constraint.name == name
    )


def _a_column_keeps_every_row_apart(
    table: Table, name: str, fields: tuple[str, ...], rows_of: dict[type[Model], int]
) -> bool:
    """Whether one column settles this constraint, whatever the others do."""
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
    """What this column holds: its declaration, what the load leaves there, or None."""
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
    literal = default.value if isinstance(default, Value) else default
    if hasattr(literal, "resolve_expression") and not isinstance(literal, Now):
        return None
    return _LeftOut(literal)


def _one_group(table: Table, paired: Paired, rows_of: dict[type[Model], int]) -> bool:
    """Whether a pairing's relation is known to spread its rows over one parent."""
    relation = cast("FanOut", table.fields[paired.relation])
    return _parents_available(table, paired.relation, relation, rows_of) == 1


def _a_flat_tree(table: Table, name: str) -> str:
    """The tree a refusal may offer to keep this constraint on its own, in words."""
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
    """Whether a tree among these columns provably keeps the constraint on its own."""
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
    """Whether one fan-out here gives no group two rows, which settles every check below."""
    return any(
        isinstance(table.fields.get(column), FanOut)
        and _no_group_of_two(table, name, column, rows_of)
        for column in fields
    )


def _no_group_of_two(
    table: Table, name: str | None, column: str, rows_of: dict[type[Model], int]
) -> bool:
    """Whether this fan-out provably gives no group two rows, under this constraint."""
    fan_out = cast("FanOut", table.fields[column])
    return (not fan_out.null or _each_null_is_its_own_group(table, name)) and _cannot_collide(
        table, column, rows_of
    )


def _check_a_fan_out_alone(
    table: Table, name: str | None, column: str, rows_of: dict[type[Model], int]
) -> None:
    """A fan-out alone under a uniqueness, which permits one row per parent."""
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
    """Whether a cap of one on this fan-out could hold every row, so a refusal may offer it."""
    parent_rows = _parents_available(table, column, cast("FanOut", table.fields[column]), rows_of)
    return parent_rows is None or table.rows <= parent_rows


def _check_a_fan_out_on_a_unique_column(table: Table, rows_of: dict[type[Model], int]) -> None:
    """A partition over a column that permits one row per parent."""
    for column, declared in table.fields.items():
        field = cast("Field[Any, Any]", table.model._meta.get_field(column))
        if isinstance(declared, FanOut) and field.unique:
            _check_a_fan_out_alone(table, None, column, rows_of)


def _what_makes_a_unique_column_new(field: Field[Any, Any], rows: int) -> str:
    """The remedy for a unique left-out column, named for its field and proven by load."""
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
    """Whether ``Sequential(1, 1)`` loads ``rows`` distinct values into this column."""
    digits = len(str(rows))
    if isinstance(field, SmallIntegerField):
        return rows <= 2**15 - 1
    if isinstance(field, DecimalField):
        return field.max_digits - field.decimal_places >= digits
    if isinstance(field, (CharField, TextField)):
        return field.max_length is None or field.max_length >= digits
    return isinstance(field, (IntegerField, FloatField, JSONField, DurationField))


def _check_a_unique_column_left_to_one_value(table: Table) -> None:
    """A ``unique=True`` column the shape leaves to a ``db_default`` of one value."""
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
    """No fan-out anywhere, and the same defect: nothing enumerates the tuple."""
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
    """Whether this fan-out provably gives no parent two rows to draw for."""
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
    """One row per group may sit inside the condition. Who is filling it?"""
    where = f"{table.model.__name__}.{column}"
    group = ", ".join(fields)
    matching = _matching(column, values)
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
    """The remedy for a draw under a condition: a ``PerParent``, where it can name a value."""
    if all(_is_literal(value) for value in values):
        no_null = (
            f" over {fanned} with no null share"
            if cast("FanOut", table.fields[fanned]).null
            else ""
        )
        return (
            f"Declare the column with PerParent({fanned!r}, last={values[0]!r}, rest=...){no_null}, "
            "which puts it on one row of each group and makes the count derived from the fan-out rather "
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
    """One row per family may sit inside the condition, and nothing can arrange that."""
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
    distinct = (
        [
            "a column of the constraint that is distinct in every row, which says so with "
            "Distinct -- Sequential does"
        ]
        if any(not _is_a_relation(table, field) for field in others)
        else []
    )
    offers = [
        f"{column} filled so that no row lands inside the condition",
        *distinct,
        _a_flat_tree(table, name),
        "a Projection with columns= and sql=",
    ]
    raise InvalidShape(
        f"{name} permits one row with {_matching(column, values)} per ({', '.join(fields)}), and "
        f"{', '.join(named)}; {where}.{column}={declared!r} {asks}. Nothing is decided per family "
        "of a tree, since a family is not a range of rows, so no row inside the condition can see "
        "whether a sibling is inside it too. Two of them sharing a group is a matter of the seed "
        "at any weight, and the load then fails inside COPY at a row number that moves when the "
        f"seed does. What a tree accepts: {'; '.join(offers[:-1])}; or {offers[-1]}."
    )


def _check_conditional_over_draws(
    table: Table,
    name: str,
    fields: tuple[str, ...],
    column: str,
    values: tuple[object, ...],
    declared: object,
) -> None:
    """A condition over a constraint no column of which is partitioned."""
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
    if any(not _is_a_relation(table, field) for field in fields):
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
    """The condition names a relation, so what it tests is a key this package chose."""
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
    """Whether a row this relation fills can hold ``value`` and count under the constraint."""
    if value is None:
        if column in fields and _each_null_is_its_own_group(table, name):
            return False
        return bool(declared.null) if isinstance(declared, FanOut) else isinstance(declared, Tree)
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
    """Whether the one value left inside can give a group two rows. A proof, or False."""
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
    """Whether a ``Paired`` and its own relation are both in this constraint."""
    return any(
        isinstance(paired, Paired)
        and paired.relation in fields
        and _its_relation_keeps_the_groups(table, name, paired)
        for paired in (table.fields.get(field) for field in fields)
    )


def _its_relation_keeps_the_groups(table: Table, name: str, paired: Paired) -> bool:
    """Whether this constraint sees the groups the pairing kept its partners distinct in."""
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
    """The remedies that would build for this declaration; the last one always does."""
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
    if any(not _is_a_relation(table, field) for field in others):
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
    """Narrowing the keys away and dropping the null share, or a cap that leaves one row."""
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


def _is_a_relation(table: Table, column: str) -> bool:
    """Whether this column is a key, which no ``Distinct`` can fill."""
    return cast("Field[Any, Any]", table.model._meta.get_field(column)).is_relation


def _may_be_left_out(table: Table, column: str) -> bool:
    """Whether leaving this column out loads a NULL, with nothing else needing it."""
    field = cast("Field[Any, Any]", table.model._meta.get_field(column))
    read = any(
        (isinstance(one, Paired) and one.relation == column)
        or (isinstance(one, Derivation) and column in one.sources)
        for one in table.fields.values()
    )
    return not read and field.null and getattr(field, "db_default", NOT_PROVIDED) is NOT_PROVIDED


def _asks(table: Table, declared: object, values: tuple[object, ...]) -> str:
    """The declaration's own arithmetic, quoted back at it when it has any."""
    if not all(_is_literal(value) for value in values):
        return "may meet the condition in any row, which nothing here can rule out"
    if isinstance(declared, Constant):
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
    """How many distinct combinations of ``fields`` this declaration can build."""
    apart = _each_null_is_its_own_group(table, name)
    total = 1
    for column in fields:
        declared = _held(table, column)
        if isinstance(declared, FanOut):
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
    """How many parents this fan-out can actually point at, or None if unknown."""
    if fan_out.parents is not None:
        return len(fan_out.parents)
    parent = cast("type[Model]", table.model._meta.get_field(column).related_model)
    return rows_of.get(parent)


def _produces_any(declared: object, values: tuple[object, ...]) -> bool | None:
    """Whether this declaration can put **any** of these in a row."""
    return _any_of(_produces(declared, value) for value in values)


def _produces(declared: object, value: object) -> bool | None:
    """Whether this declaration can put ``value`` in a row: yes, no, or unknown."""
    if isinstance(declared, Constant):
        return _meets(declared.value(0, 0.0), value)
    if isinstance(declared, Categorical):
        return _any_of(_meets(candidate, value) for candidate in declared.shares())
    return None if _is_declaration(declared) else _meets(declared, value)


def _meets(held: object, member: object) -> bool | None:
    """Whether a row holding ``held`` meets one member of a condition: yes, no, or unknown."""
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
    """Whether this column's value is decided by the row index and nothing else."""
    return _is_declaration(declared) and not isinstance(declared, Derivation)


def _is_declaration(declared: object) -> bool:
    """Whether this is a distribution rather than a plain column value."""
    return callable(getattr(type(declared), "value", None))


def _condition(condition: Q) -> tuple[str, tuple[object, ...]] | None:
    """The column a condition tests and the values that satisfy it, or None."""
    if condition.negated:
        return None
    if condition.connector == Q.OR:
        return _union(_decode(child) for child in condition.children)
    return _decode(condition.children[0]) if len(condition.children) == 1 else None


def _decode(child: object) -> tuple[str, tuple[object, ...]] | None:
    """One branch of a condition: a nested ``Q``, or a ``(lookup, value)`` pair."""
    if isinstance(child, Q):
        return _condition(child)
    if not isinstance(child, tuple) or len(child) != 2:
        return None
    lookup, value = child
    column, _, suffix = str(lookup).partition("__")
    if suffix == "isnull":
        return column, (None,) if value else (_NOT_NULL,)
    if suffix in ("", "exact"):
        return column, _members((value,))
    if suffix != "in" or not isinstance(value, (list, tuple, set, frozenset)):
        return None
    return column, _members(
        tuple(value) if isinstance(value, (list, tuple)) else tuple(sorted(value, key=repr))
    )


def _members(written: tuple[object, ...]) -> tuple[object, ...]:
    """The values a condition names, read as what the database compares."""
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
    """Every branch's values, if every branch decodes and names one column."""
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
