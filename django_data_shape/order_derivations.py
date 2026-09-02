"""Deciding which column to compute first."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from django_data_shape.derivations.derivation import Derivation
from django_data_shape.derivations.scope import Scope
from django_data_shape.invalid_shape import InvalidShape


def order_derivations(model: str, fields: Mapping[str, object]) -> tuple[str, ...]:
    """The declared derivations, dependencies first.

    **Column order is not computation order**, and conflating them is the same
    mistake this package has now met four times -- assignment order against
    emission order for placement, partition order against emission order for
    fan-out, load order against schema order for tables, and now this.
    ``Table.columns()`` sorts by name so that two declarations differing only in
    keyword order produce the same ``COPY`` statement and hash to the same cache
    key. That order says nothing at all about what depends on what, and a
    derivation reading a column that has not been computed yet would read the
    slot's initial value rather than fail.

    Only ``Scope.ROW`` sources create an edge. A parent source names a column of
    another table, already loaded; a rank source names a draw, which depends on
    nothing. So the graph is small and usually empty, and its shape is entirely
    the caller's business rather than the model's.

    A cycle is refused by name and at declaration time, because the alternative
    is a shape that generates for a while and then either recurses forever or --
    worse -- reads a partly-filled row and loads it.
    """
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(name: str, trail: tuple[str, ...]) -> None:
        mark = state.get(name, 0)
        if mark == 2:
            return
        if mark == 1:
            cycle = " -> ".join((*trail, name))
            raise InvalidShape(
                f"{model} cannot compute {name}, because its derivations depend on each other "
                f"in a cycle: {cycle}. One of them has to be computed first, and a cycle means "
                "none of them can."
            )
        state[name] = 1
        derivation = cast("Derivation", fields[name])
        # cast rather than a guard: visit is only ever reached for a name whose
        # declaration was already found to be a Derivation, either by the loop
        # below or by the isinstance check in this branch.
        if derivation.scope is Scope.ROW:
            for source in derivation.sources:
                if isinstance(fields.get(source), Derivation):
                    visit(source, (*trail, name))
        state[name] = 2
        order.append(name)

    # Sorted so the order two independent derivations come out in is a property
    # of the declaration rather than of dictionary insertion, which is what a
    # shape hash will later have to depend on.
    for name, declared in sorted(fields.items()):
        if isinstance(declared, Derivation):
            visit(name, ())
    return tuple(order)
