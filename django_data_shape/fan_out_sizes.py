"""Answering which parents the children went to, without aggregating them."""

from __future__ import annotations

from typing import cast

from django.db import DEFAULT_DB_ALIAS, connections
from django.db.models import Field, Model

from django_data_shape.children_per_parent import ChildrenPerParent
from django_data_shape.fan_out import FanOut
from django_data_shape.invalid_shape import InvalidShape
from django_data_shape.resolve_fan_out import resolve_fan_out
from django_data_shape.shape import Shape
from django_data_shape.table import Table
from django_data_shape.world_changed import WorldChanged


def fan_out_sizes(
    shape: Shape, model: type[Model], field: str, *, using: str = DEFAULT_DB_ALIAS
) -> ChildrenPerParent:
    """How many children each parent got, for one declared fan-out.

    The reason a fan-out is a **partition of the child key range** rather than a
    per-child draw is that a partition can be inverted -- and until this
    function existed the inversion was not reachable from outside, so a caller
    who declared a skew had to ``GROUP BY`` the child table to find out where
    its head was. That is an aggregate over the whole world, run inside the
    session that is about to measure a query plan, to recover something the
    declaration already knew.

    ::

        counts = fan_out_sizes(shape, Order, "company")

        whale, orders = counts.ranked()[0]
        assert counts[whale] == orders
        assert counts.childless()

    **It is recomputed, not remembered**, and that is the design rather than an
    implementation detail. The partition is a pure function of the declaration,
    the seed and the parent's primary keys, so it is derived here through the
    very code the build runs --
    :func:`~django_data_shape.resolve_fan_out.resolve_fan_out` -- rather than
    through a second implementation that would agree with the first only until
    one of them changed.

    That is also the answer to the question this function was written to
    survive. **A cached build skips generation entirely**, so nothing carried
    off a build result could be available on that path: a template database is
    cloned and no row is ever generated. Recomputation does not care. The clone
    holds the parent table, the declaration holds everything else, and
    :func:`~django_data_shape.template_database.template_database` keys its
    cache on this package's own version, so a template built by a release that
    drew differently is never the one being read. The inversion is therefore
    answerable from a cache hit, a fresh build, or a database somebody restored
    from a dump, and it costs one query over the parents rather than a scan of
    the children.

    The one thing recomputation depends on is that **the parent table still
    holds the parents the children were spread across**. Where the parent is
    declared in the same shape -- which every cacheable shape is, since a
    template is built into a freshly migrated database and a fan-out over an
    empty parent is refused -- that is checked here, and a mismatch raises
    :class:`~django_data_shape.world_changed.WorldChanged` rather than returning
    a plausible partition of a world that never existed. Where the parents were
    built outside the shape, by the ORM or a factory, there is nothing to check
    against and nothing is claimed: the answer describes the parents that are
    there now, so ask before a test starts creating more of them.

    Costs one ``SELECT`` over the parent table. Everything else is arithmetic
    over the parent count, which is the asymmetry worth having -- fifty
    thousand parents against two million children is the shape this package is
    built for, and it is the child table an aggregate would have to read.
    """
    declared = {table.db_table: table for table in shape.tables}

    declaration = declared.get(model._meta.db_table)
    if declaration is None:
        raise InvalidShape(
            f"This shape declares no table for {model.__name__}, so it has no fan-out over "
            f"{field} to report. It declares: "
            f"{', '.join(sorted(declared)) or 'nothing'}."
        )
    if not isinstance(declaration, Table):
        raise InvalidShape(
            f"{model.__name__} is declared as a Projection, whose foreign keys are copied along "
            "the join it selects from rather than spread by a fan-out. There is no partition to "
            "invert; the counts it produces are a property of the tables it reads."
        )

    fan_out = declaration.fields.get(field)
    if not isinstance(fan_out, FanOut):
        raise InvalidShape(
            f"{model.__name__}.{field} is not declared with a FanOut in this shape, and only a "
            "fan-out partitions children across parents. Its declared fields are: "
            f"{', '.join(sorted(declaration.fields)) or 'none'}."
        )

    # Safe by the time it runs: Table refuses a FanOut on a column that is not a
    # relation at declaration time, so a name reaching here is one relations()
    # returns.
    relation = dict(declaration.relations())[field]
    parent = cast("type[Model]", cast("Field[object, object]", relation).related_model)

    plan = resolve_fan_out(
        fan_out,
        parent,
        declaration.rows,
        shape.seed,
        declaration.db_table,
        field,
        connections[using],
    )
    keys = plan.parent_keys()

    parent_declaration = declared.get(parent._meta.db_table)
    if isinstance(parent_declaration, Table) and len(keys) != parent_declaration.rows:
        raise WorldChanged(
            f"{parent.__name__} holds {len(keys)} rows and this shape declares "
            f"{parent_declaration.rows}, so the fan-out for {model.__name__}.{field} would be "
            "spread over parents the children were never spread across. The counts are derived "
            "from the parent keys rather than counted off the child table, so an answer here "
            "would describe a world that was never built. The usual causes are a test that "
            "created more parents before asking, and a transactional test that truncated the "
            "tables a session-scoped fixture built."
        )

    return ChildrenPerParent(keys, plan.sizes(), fan_out.null)
