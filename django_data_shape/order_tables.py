"""Deciding which table to fill first."""

from __future__ import annotations

from typing import cast

from django.db.models import Model

from django_data_shape.fan_out import FanOut
from django_data_shape.invalid_shape import InvalidShape
from django_data_shape.projection import Projection
from django_data_shape.table import Table


def order_tables(tables: tuple[Table | Projection, ...]) -> tuple[Table | Projection, ...]:
    """Everything a declaration reads, before the declaration itself.

    Not because the database demands it. Django creates every PostgreSQL foreign
    key ``DEFERRABLE INITIALLY DEFERRED``, so inside one transaction the checks
    fire at commit and any order would satisfy them.

    The real reason is that a declaration **reads the tables it depends on**, and
    a table with no rows yet has none to read. A fan-out reads its parent's keys;
    a :class:`~django_data_shape.projection.Projection` selects from two tables
    outright. Ordering is therefore a correctness requirement of this package's
    own design rather than of the schema -- which is worth stating, because the
    schema is where a reader would look for it and would find nothing.

    **A projected table may be a fan-out parent, and that is what this pass is
    for.** A projection runs after the tables it reads, so the temptation is to
    run every projection last and be done; that would quietly forbid a table
    fanning out over a projected one, and forbid it by a scheduling accident
    rather than by anything true about the data. Sorting the whole declaration
    graph at once costs nothing here and removes the restriction: a projection
    is just another node with edges to what it reads.

    **An edge is a claim, and only a declaration gets to make one.** A statement
    this package did not write -- ``Projection(..., sql=...)`` without
    ``reads=`` -- names nothing, because nothing here parses SQL, and the only
    safe reading of "this could select from anything" is to run it as late as
    possible. That is a *preference*, and expressing it as edges to every other
    declaration is what made this pass report a cycle that did not exist: a
    table fanning out over a raw projection was told ``A -> B -> A`` when the
    two form a chain, with no ``after=`` anywhere for the caller to correct it
    with. So the preference is expressed as a visit order instead. The graph
    holds declared edges only, and the sweep below reaches every declaration
    that says what it reads before it reaches any statement that does not --
    which puts an opaque projection last where nothing needs it, and directly
    before its dependents where something does. A preference cannot contradict
    another preference; an edge can, and did.

    ``reads=`` is how a raw statement rejoins the graph properly. Without it,
    being ordered early enough for a dependent may put it *before* a table it
    selects from, which the build catches as a projection that inserted
    nothing -- a loud failure, but one the caller can now prevent by naming its
    inputs.

    A cycle is refused by name, and now only a declared one can be: two
    declarations that each say they read the other cannot both be filled second,
    and no amount of deferring changes that. Because the refusal is structural
    and reads nothing but the declarations, it is raised when the
    :class:`~django_data_shape.shape.Shape` is built rather than when the build
    starts.
    """
    by_model: dict[type[Model], Table | Projection] = {table.model: table for table in tables}
    ordered: list[Table | Projection] = []
    state: dict[Table | Projection, int] = {}

    def visit(table: Table | Projection, trail: tuple[Table | Projection, ...]) -> None:
        mark = state.get(table, 0)
        if mark == 2:
            return
        if mark == 1:
            names = " -> ".join(t.model.__name__ for t in (*trail, table))
            raise InvalidShape(
                f"These tables read each other in a cycle: {names}. One of them has to be "
                "filled first, and a cycle means neither can be."
            )
        state[table] = 1
        # No self-edge to skip. A self-referential ``FanOut`` is refused by
        # ``Table``, a projection reading the table it fills is refused by
        # ``Projection`` -- through ``per``/``copying`` and through ``reads=``
        # alike -- so guarding against one here would be a branch no declaration
        # can reach. The day self-referential trees arrive, this is the line
        # that has to learn about them.
        for dependency in _dependencies(table, by_model):
            visit(dependency, (*trail, table))
        state[table] = 2
        ordered.append(table)

    # Two sweeps rather than one, and this is where "as late as possible" is
    # decided. Everything that says what it reads goes first, so a statement
    # that says nothing is reached only after them -- and is then already
    # placed, if something declared it reads this table and pulled it in early.
    for table in tables:
        if not _is_opaque(table):
            visit(table, ())
    for table in tables:
        visit(table, ())
    return tuple(ordered)


def _dependencies(
    table: Table | Projection, by_model: dict[type[Model], Table | Projection]
) -> list[Table | Projection]:
    """The declarations ``table`` says it has to be filled after.

    A model that is not in the shape is a table the caller built themselves,
    which is the supported hybrid: the ORM for the small tables, this package
    for the large ones. Its rows are read when the time comes, and nothing here
    has to order it.

    One expression covers both kinds of projection because ``reads`` already
    does: a derived one answers with the two models it joins, a raw one with
    whatever ``reads=`` declared, and a raw one that declared nothing answers
    with nothing and gets no edges at all.
    """
    if isinstance(table, Projection):
        return [by_model[model] for model in table.reads if model in by_model]

    parents: list[Table | Projection] = []
    for name, declared in table.fields.items():
        if not isinstance(declared, FanOut):
            continue
        # cast, not a guard: related_model is Optional on the generic field
        # descriptor, but Table has already refused a FanOut on anything that
        # is not a relation. Branching on None here would add a path no test
        # could reach, which is how 100% branch coverage stops being achievable
        # honestly.
        parent = cast("type[Model]", table.model._meta.get_field(name).related_model)
        if parent in by_model:
            parents.append(by_model[parent])
    return parents


def _is_opaque(table: Table | Projection) -> bool:
    """Whether this declaration's inputs are unknowable from the declaration."""
    return isinstance(table, Projection) and not table.reads
