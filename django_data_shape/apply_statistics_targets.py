"""Making the planner keep as much of a declared shape as it was asked for."""

from __future__ import annotations

from typing import Any, cast

from django.db.models import Field

from django_data_shape.distributions.bounded import Bounded
from django_data_shape.invalid_shape import InvalidShape
from django_data_shape.projection import Projection
from django_data_shape.table import Table

# One query, and it normalises in SQL rather than in Python on purpose.
# ``attstattarget`` says "use the server default" as -1 through PostgreSQL 16 and
# as NULL from 17, so the obvious Python test needs two branches of which exactly
# one is reachable on any given server -- and a branch no job can execute is a
# branch this package's own coverage gate cannot see. NULLIF folds the old
# spelling into the new one and COALESCE resolves both to the same number.
_EFFECTIVE_TARGETS = """
SELECT a.attname,
       COALESCE(NULLIF(a.attstattarget, -1), current_setting('default_statistics_target')::int)
FROM pg_attribute a
WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
"""


def apply_statistics_targets(connection: Any, declaration: Table | Projection) -> None:
    """Set the declared per-column targets, and refuse a shape the planner could not record.

    ``ANALYZE`` has shipped since 0.1.0 and it is only half the story. What it
    gathers is bounded by each column's *statistics target*: PostgreSQL keeps at
    most that many most-common values and that many histogram bounds, and samples
    300 times as many rows to find them. Everything past the target is collapsed
    into one residual frequency, so a column with more distinct values than its
    target has a shape the planner cannot see -- and this package exists to make
    a declared shape visible.

    **The target is declared, never inferred.** This function reads the
    distributions only to refuse, and that distinction is the whole design.
    Raising a target on the caller's behalf would be this package choosing how
    the planner sees a column, silently, on evidence the declaration does not
    contain: a hundred-value skew wants a hundred buckets in a table where those
    hundred values are the query's predicate and wants far fewer where they are
    not, and nothing in a distribution says which. So a shape that gets a
    hundred-bucket histogram because ``default_statistics_target`` happens to be
    a hundred is treated as different from one that asked for it -- not by
    guessing at the second, but by making the first impossible to hold by
    accident. Either the declaration can be recorded, or the build stops and
    names the column.

    **Which is a refusal that cannot happen at declaration time**, like
    :class:`~django_data_shape.derivations.given.Given`'s missing case and a
    :class:`~django_data_shape.projection.Projection` that inserts nothing. The
    number it compares against lives in the server -- a column carries a target
    set by a migration, and everything else falls back to a setting an operator
    can change -- so the declaration alone cannot know it. It is still raised
    before a single row is generated rather than after the load, because a
    refusal that costs a two-million-row ``COPY`` first is a refusal nobody
    thanks you for.

    **The order matters and is easy to get backwards.** A target changed after
    ``ANALYZE`` has run does nothing at all until the next one, exactly as
    ``ANALYZE`` before a load leaves stale statistics behind. So this runs before
    the rows, and the ``ANALYZE`` at the end of the build is the one that reads
    it -- the ordering is owned by the library for the same reason the rest of
    the sequence is.

    Nothing happens on another backend. The branch is on the connection's vendor
    rather than on a failed statement, so it is covered by passing a vendor
    rather than by running the suite on the backend it skips.
    """
    if connection.vendor != "postgresql":
        return

    quote = connection.ops.quote_name
    columns = {
        name: _column(declaration.model._meta.get_field(name)) for name in declaration.statistics
    }
    with connection.cursor() as cursor:
        cursor.execute(_EFFECTIVE_TARGETS, [declaration.db_table])
        effective = {column: target for column, target in cursor.fetchall()}
        if isinstance(declaration, Table):
            _refuse_what_cannot_be_recorded(declaration, effective)
        for name, target in sorted(declaration.statistics.items()):
            # The target is interpolated rather than bound. ALTER TABLE takes no
            # placeholders -- the value is part of the utility statement's own
            # grammar -- and it is an integer inside PostgreSQL's own range,
            # checked when the declaration was made.
            cursor.execute(
                f"ALTER TABLE {quote(declaration.db_table)} "
                f"ALTER COLUMN {quote(columns[name])} SET STATISTICS {target}"
            )


def _refuse_what_cannot_be_recorded(table: Table, effective: dict[str, int]) -> None:
    """Refuse a bounded distribution with more values than its column can hold.

    Only a :class:`~django_data_shape.distributions.bounded.Bounded`
    distribution can be checked, which is the second job that protocol has done
    and the reason it was worth having: a distribution drawing from a continuous
    range simply does not answer, and is left alone rather than guessed at. The
    same is true of a projected table, which declares no distributions at all --
    its skew is whatever the table it copies from had.
    """
    meta = table.model._meta
    for name in sorted(table.fields):
        distribution = table.fields[name]
        if not isinstance(distribution, Bounded):
            continue
        values = distribution.distinct_values()
        column = _column(meta.get_field(name))
        target = table.statistics.get(name, effective[column])
        if values <= target:
            continue
        raise InvalidShape(
            f"{table.model.__name__}.{name} declares {values} distinct values and that column's "
            f"statistics target is {target}, so the planner would record {target} of them and "
            "estimate the rest from a single residual frequency. The declared shape would be "
            "built and then not seen, which is the state this package exists to expose rather "
            f"than to produce. Ask for it -- statistics={{{name!r}: {values}}} on this table -- "
            "or declare fewer values."
        )


def _column(field: object) -> str:
    """The column one declared field writes to.

    ``cast`` twice rather than a guard, and for the reason the same helper in
    ``projection.py`` gives: ``get_field`` is typed to include reverse
    relations, which every name reaching here has already been checked not to
    be, and ``column`` is Optional only in the stubs. Branching on either would
    add a path no declaration can reach, which is how 100% branch coverage stops
    being achievable honestly.
    """
    return cast("str", cast("Field[Any, Any]", field).column)
