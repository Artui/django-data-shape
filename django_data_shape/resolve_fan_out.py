"""Turning a declared fan-out into a partition over the parent keys that exist."""

from __future__ import annotations

import numbers
from decimal import Decimal
from typing import Any

from django.db.models import Model

from django_data_shape.fan_out import FanOut
from django_data_shape.fan_out_plan import FanOutPlan
from django_data_shape.invalid_shape import InvalidShape
from django_data_shape.utils import draw, field_stream


def resolve_fan_out(
    fan_out: FanOut,
    parent: type[Model],
    rows: int,
    seed: int,
    table: str,
    field: str,
    connection: Any,
    parent_fields: tuple[str, ...] = (),
) -> FanOutPlan:
    """Read the parent's real keys and partition ``rows`` children across them.

    The keys are **queried, not assumed**. An earlier design took them to be
    the dense ``1..N`` range this package assigns, which is wrong in the case
    that matters most: a project builds its fifty companies with the ORM -- where
    the row count is small and the ORM is the right tool -- and asks this package
    only for the two million orders. Their keys are then whatever the sequence
    handed out, and a child pointing at ``1..50`` would point at nothing.

    Reading them is also what makes referential integrity hold by construction
    rather than by validation: every key emitted came out of the parent table.

    ``parent_fields`` names the parent columns a derivation on the child reads.
    They come back beside the keys, in the same order, so a child reaches its
    parent's values through the partition rather than through a query of its
    own. **The same correction applies to them as to the keys**: they are read
    out of the parent table rather than recomputed from the parent's
    declaration, so a parent this package never built works identically.
    """
    keys, parent_values = _read_parents(parent, parent_fields, connection, fan_out.parents)
    _require_every_named_parent(fan_out.parents, keys, parent, table, field)
    keys, parent_values = _in_declared_order(fan_out.parents, keys, parent_values)

    if not keys and rows:
        raise InvalidShape(
            f"{table}.{field} fans out over {parent.__name__}, which has no rows. "
            "Load the parent first, or declare it in the same shape so it is built before "
            "this table."
        )

    sizes = _sizes(fan_out, keys, rows, seed, table, field)
    starts: list[int] = []
    running = 0
    for size in sizes:
        starts.append(running)
        running += size

    return FanOutPlan(
        keys=keys,
        starts=starts,
        rows=rows,
        null_stream=field_stream(seed, table, f"{field}:null"),
        null_share=fan_out.null,
        interleave=fan_out.placement == "arrival",
        parent_values=parent_values,
    )


def _in_declared_order(
    named: tuple[object, ...] | None,
    keys: list[int],
    parent_values: dict[str, list[object]],
) -> tuple[list[int], dict[str, list[object]]]:
    """Put named parents back in the order the declaration named them.

    **The partition has to be a function of the declaration, and this is what
    makes it one.** Keys arrive ordered by primary key because that is how they
    are read, and the sizes below are assigned by *position* -- so without this
    the weights followed the sort order of values nobody wrote down. With
    integer keys that is merely surprising; with the UUID keys a factory row has
    on a modern schema it means **the same shape builds differently every run**.
    Measured before this existed: one declaration gave the first-named parent 5,
    11 or 79 rows across twelve builds, because the sort order moved with the
    keys.

    That is the promise the whole package rests on -- two builds of one shape
    agree, which is why ``UuidKeys`` derives rather than draws -- so a narrowing
    that broke it was a defect rather than a preference.

    **The weights are still scattered across positions**, and the reasoning for
    that survives unchanged: it is a caller's *order* that decides which key
    lands where, not their key values, so nothing here correlates a parent's key
    with its child count. What it does mean is that reversing the list is a
    different declaration, which is what a reader writing one would expect.

    Parent values move with their keys, or a derivation would read the right
    column off the wrong row.
    """
    if named is None:
        return keys, parent_values
    # `dict[object, int]` rather than the `list[int]` this module annotates its
    # keys with, because that annotation is not true: a primary key is whatever
    # the model declares, and the UUID case is exactly the one this function
    # exists for. Widening it everywhere is a change of its own; narrowing here
    # would be a lie that type-checks.
    position: dict[object, int] = {key: index for index, key in enumerate(keys)}
    order = [position[key] for key in named]
    return (
        [keys[index] for index in order],
        {name: [values[index] for index in order] for name, values in parent_values.items()},
    )


def _require_every_named_parent(
    named: tuple[object, ...] | None,
    keys: list[int],
    parent: type[Model],
    table: str,
    field: str,
) -> None:
    """Every key ``parents=`` named has to be a row, or the narrowing is a lie.

    The database does the narrowing, so a key that matches nothing simply does
    not come back -- and the rows that would have pointed at it go to whatever
    else was named instead, or the table comes out empty if it was the only one.
    Both are silent, and both produce a world the declaration does not describe.

    The likely causes are worth the message: a primary key from another test, a
    factory whose row was rolled back, or a list built from a queryset that was
    filtered differently from the one in the reader's head. None of them is
    visible in the shape.

    **Every missing key, not the first.** A list built from the wrong queryset is
    wrong in several places at once, and finding that out one round trip at a
    time is the slow way to learn it.
    """
    if named is None:
        return
    missing = [key for key in named if key not in set(keys)]
    if not missing:
        return
    raise InvalidShape(
        f"{table}.{field} fans out over {parent.__name__} and names "
        f"{', '.join(repr(key) for key in missing)} in parents=, which "
        f"{'is not a row' if len(missing) == 1 else 'are not rows'} of "
        f"{parent._meta.db_table}. A key that matches nothing is not an empty share -- the rows "
        "that would have gone to it go to the other parents named instead, and to none at all if "
        "it was the only one, so the world would be built and quietly not be the declared one. "
        "The usual causes are a key from another test, a factory row that was rolled back, and a "
        "list built from a queryset filtered differently from the one you meant."
    )


def _read_parents(
    parent: type[Model],
    parent_fields: tuple[str, ...],
    connection: Any,
    named: tuple[object, ...] | None = None,
) -> tuple[list[int], dict[str, list[object]]]:
    """The parent's keys, and any of its columns a child derives from.

    Two routes, and the split is not laziness. The keys alone come back through
    one hand-written statement, which is what lets every branch of the partition
    be covered by a stub connection -- the same reasoning as the backend gate,
    where logic reachable only through a real database is logic the coverage
    gate cannot see.

    Values cannot take that route, because **a raw column is not a Python
    value**: a cursor bypasses the field's own ``from_db_value``, and a key is
    the one column where that never shows. Measured rather than assumed --
    SQLite hands a raw ``DateTimeField`` back **naive** where the ORM hands back
    an aware datetime, so ``After`` would compute an offset from a value six
    hours from the one the application reads, under a warning nobody sees in a
    passing run; and a ``JSONField`` comes back as text rather than as the dict
    it is. Any field with a converter of its own is the same case, on every
    backend. The ORM route hands a derivation the value the application would
    have read.
    """
    if not parent_fields:
        pk_column = parent._meta.pk.column
        quote = connection.ops.quote_name
        # The narrowing is a predicate rather than a filter applied afterwards,
        # so a shape pinned to one tenant reads one row instead of every key in
        # a table it is about to ignore. The keys go out as parameters, which is
        # what keeps a caller's own primary key off the statement text.
        # One placeholder per key rather than a single one for the whole list.
        # A driver is free to adapt a Python list to its own array syntax --
        # psycopg 3 does, and `IN '{1,2}'` is a syntax error -- and `= ANY(%s)`,
        # which would take the array, is PostgreSQL's alone. This package
        # generates on every backend, so the portable form is the one to write.
        where = (
            ""
            if named is None
            else f" WHERE {quote(pk_column)} IN ({', '.join(['%s'] * len(named))})"
        )
        params = None if named is None else list(named)
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {quote(pk_column)} FROM {quote(parent._meta.db_table)}"
                f"{where} ORDER BY {quote(pk_column)}",
                params,
            )
            return [row[0] for row in cursor.fetchall()], {}

    # _base_manager rather than _default_manager: a project's default manager
    # may filter, and a fan-out that silently skipped the parents somebody's
    # manager hides would point children at a subset while reporting the whole.
    # It is the manager Django itself uses to follow a relation, for the same
    # reason.
    queryset = parent._base_manager.using(connection.alias)
    if named is not None:
        queryset = queryset.filter(pk__in=list(named))
    records = list(queryset.order_by("pk").values_list("pk", *parent_fields))
    return (
        [record[0] for record in records],
        {
            name: [record[index + 1] for record in records]
            for index, name in enumerate(parent_fields)
        },
    )


def _sizes(
    fan_out: FanOut, keys: list[int], rows: int, seed: int, table: str, field: str
) -> list[int]:
    """How many children each parent gets, summing to exactly ``rows``.

    The largest-remainder method rather than plain rounding, because rounding
    each share independently does not add up: a thousand parents rounded down
    lose hundreds of rows, and a partition that does not cover the range would
    leave children pointing past the end of it.
    """
    # Nothing to place means nothing to weigh. Reaching the zero-total refusal
    # below with an empty table would refuse a shape that is merely empty, which
    # is a legitimate thing to declare -- it is what a parent with no children
    # looks like.
    if rows == 0:
        return [0] * len(keys)

    weight_stream = field_stream(seed, table, f"{field}:weight")
    childless_stream = field_stream(seed, table, f"{field}:childless")

    weights: list[float] = []
    for index in range(len(keys)):
        if fan_out.childless and draw(childless_stream, index) < fan_out.childless:
            weights.append(0.0)
            continue
        weight = fan_out.sizes.value(index, draw(weight_stream, index))
        # Checked rather than coerced. A size distribution handing back
        # something that is not a number is a declaration mistake, and the
        # alternative is a TypeError from inside the partition arithmetic that
        # names neither the table nor the field.
        #
        # The test is a numeric *tower* and not a list of types, because the
        # narrow version refused the spelling this package recommends:
        # `Uniform(1, 10, places=0)` rounds through Decimal, and `places=0` is
        # the natural way to say that a fan-out size is a count. It was refused
        # by `isinstance(weight, (int, float))` while the next line already did
        # `float(weight)` -- stricter than the arithmetic it was protecting.
        #
        # `numbers.Real` alone does not reach it: Decimal is `numbers.Number`
        # and deliberately not `Real`, because mixing it with float is not
        # something the tower wants to promise. So Decimal is named beside it,
        # and Fraction and numpy scalars come along for free.
        #
        # `bool` stays out. It is an int and it is `Real`, and a distribution
        # handing back True has almost certainly been written for a different
        # column.
        if isinstance(weight, bool) or not isinstance(weight, (numbers.Real, Decimal)):
            raise InvalidShape(
                f"{table}.{field} needs numeric fan-out sizes, but "
                f"{fan_out.sizes!r} produced {weight!r}. A fan-out size is a count of children "
                "per parent, so the distribution weighing it has to produce numbers -- int, "
                "float, Decimal and Fraction all work, and the weights are normalised so their "
                "scale does not matter."
            )
        weights.append(float(weight))

    total = sum(weights)
    if total <= 0:
        raise InvalidShape(
            f"{table}.{field} gives all {len(keys)} of its parents a weight of zero, so there "
            f"is nowhere to put {rows} rows. Lower childless, or widen the size distribution."
        )

    exact = [weight / total * rows for weight in weights]
    sizes = [int(value) for value in exact]
    remainder = rows - sum(sizes)
    # Hand the leftover rows to the parents with the largest fractional parts:
    # the ones that were closest to earning another row.
    order = sorted(range(len(sizes)), key=lambda i: exact[i] - sizes[i], reverse=True)
    for index in order[:remainder]:
        sizes[index] += 1
    return sizes
