"""Deciding which table to load first."""

from __future__ import annotations

from typing import cast

from django.db.models import Model

from django_data_shape.fan_out import FanOut
from django_data_shape.invalid_shape import InvalidShape
from django_data_shape.table import Table


def order_tables(tables: tuple[Table, ...]) -> tuple[Table, ...]:
    """Parents before children, for the reason that is easy to get wrong.

    Not because the database demands it. Django creates every PostgreSQL foreign
    key ``DEFERRABLE INITIALLY DEFERRED``, so inside one transaction the checks
    fire at commit and any order would satisfy them.

    The real reason is that a fan-out **reads the parent's keys**, and a table
    with no rows yet has none. Ordering is therefore a correctness requirement
    of this package's own design rather than of the schema -- which is worth
    stating, because the schema is where a reader would look for it and would
    find nothing.

    A cycle is refused by name. Two tables that each fan out over the other
    cannot both be loaded second, and no amount of deferring changes that.
    """
    by_model = {table.model: table for table in tables}
    ordered: list[Table] = []
    state: dict[Table, int] = {}

    def visit(table: Table, trail: tuple[Table, ...]) -> None:
        mark = state.get(table, 0)
        if mark == 2:
            return
        if mark == 1:
            names = " -> ".join(t.model.__name__ for t in (*trail, table))
            raise InvalidShape(
                f"These tables fan out over each other in a cycle: {names}. One of them has to "
                "be loaded first, and a cycle means neither can be."
            )
        state[table] = 1
        for name, declared in table.fields.items():
            if not isinstance(declared, FanOut):
                continue
            # cast, not a guard: related_model is Optional on the generic field
            # descriptor, but Table has already refused a FanOut on anything that
            # is not a relation. Branching on None here would add a path no test
            # could reach, which is how 100% branch coverage stops being
            # achievable honestly.
            parent = cast("type[Model]", table.model._meta.get_field(name).related_model)
            # A parent outside the shape is a table the caller built themselves,
            # which is the supported hybrid: the ORM for the small tables, this
            # package for the large ones. Its keys get read at resolve time.
            declared_parent = by_model.get(parent)
            if declared_parent is not None and declared_parent is not table:
                visit(declared_parent, (*trail, table))
        state[table] = 2
        ordered.append(table)

    for table in tables:
        visit(table, ())
    return tuple(ordered)
