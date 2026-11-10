from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.derivations.derivation import Derivation
from django_data_shape.derivations.scope import Scope


def order_derivations(model: str, fields: Mapping[str, object]) -> tuple[str, ...]:
    """The declared derivations, dependencies first; column order is not computation order."""
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
        # A cast rather than a guard: visit only reaches names already known to be Derivations.
        if derivation.scope is Scope.ROW:
            for source in derivation.sources:
                if isinstance(fields.get(source), Derivation):
                    visit(source, (*trail, name))
        state[name] = 2
        order.append(name)

    for name, declared in sorted(fields.items()):
        if isinstance(declared, Derivation):
            visit(name, ())
    return tuple(order)
