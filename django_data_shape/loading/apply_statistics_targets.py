from __future__ import annotations

from typing import Any, cast

from django.db.models import Field

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.declaration.projection import Projection
from django_data_shape.declaration.table import Table
from django_data_shape.distributions.bounded import Bounded

# One query, and it normalises in SQL rather than in Python on purpose.
# ``attstattarget`` says "use the server default" as -1 through PostgreSQL 16 and
# as NULL from 17, so the obvious Python test needs two branches of which exactly
# one is reachable on any given server -- and a branch no job can execute is a
# branch this package's own coverage gate cannot see. NULLIF folds the old
# spelling into the new one and COALESCE resolves both to the same number.
#
# The table is bound already quoted. ``regclass`` input is parsed as an
# identifier, so the raw ``db_table`` would fold its capitals to lower case and
# split it at a dot: a table that does not exist, or the columns of one that
# does under the folded name. Quoted by the connection, it names the table
# exactly as Django's CREATE TABLE named it -- including a ``db_table`` written
# already quoted, such as ``'"schema"."table"'``, which ``quote_name`` passes
# through unchanged there and here alike.
_EFFECTIVE_TARGETS = """
SELECT a.attname,
       COALESCE(NULLIF(a.attstattarget, -1), current_setting('default_statistics_target')::int)
FROM pg_attribute a
WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
"""


def apply_statistics_targets(connection: Any, declaration: Table | Projection) -> None:
    """Set the declared per-column targets, and refuse a shape the planner could not record."""
    if connection.vendor != "postgresql":
        return

    quote = connection.ops.quote_name
    columns = {
        name: _column(declaration.model._meta.get_field(name)) for name in declaration.statistics
    }
    with connection.cursor() as cursor:
        cursor.execute(_EFFECTIVE_TARGETS, [quote(declaration.db_table)])
        effective = {column: target for column, target in cursor.fetchall()}
        if isinstance(declaration, Table):
            _refuse_what_cannot_be_recorded(declaration, effective)
        if declaration.statistics:
            connection.check_constraints()
        for name, target in sorted(declaration.statistics.items()):
            cursor.execute(
                f"ALTER TABLE {quote(declaration.db_table)} "
                f"ALTER COLUMN {quote(columns[name])} SET STATISTICS {target}"
            )


def _refuse_what_cannot_be_recorded(table: Table, effective: dict[str, int]) -> None:
    """Refuse a bounded distribution with more values than its column can hold."""
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
    """The column one declared field writes to."""
    return cast("str", cast("Field[Any, Any]", field).column)
