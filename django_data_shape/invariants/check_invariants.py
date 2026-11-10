from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from django.db.models import Model, Q

from django_data_shape.invariants.invariant import Invariant
from django_data_shape.invariants.invariant_violated import InvariantViolated

# How many offending rows a failure quotes. Enough to see the shape of the
# problem -- one is a fluke, five is a pattern -- and few enough that a rule
# broken by a million rows still produces a message somebody reads rather than
# scrolls past. One more than this is fetched, so the message can say there
# were more without counting them: a rule broken by every row of a two-million
# row table must not be answered by pulling two million rows back into Python.
_SAMPLE = 5


def check_invariants(connection: Any, invariants: Sequence[Invariant]) -> None:
    """Run every rule, and raise on the first that finds a row."""
    for invariant in invariants:
        statement = invariant.sql
        if statement is not None:
            _check_sql(connection, invariant.name, statement)
        else:
            _check_queryset(
                connection,
                invariant.name,
                cast("type[Model]", invariant.model),
                cast("Q", invariant.violated_by),
            )


def _check_sql(connection: Any, name: str, statement: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(statement)
        offenders = cursor.fetchmany(_SAMPLE + 1)
    if offenders:
        raise InvariantViolated(
            f"The invariant {name!r} was broken. Its statement returns the rows that are wrong, "
            f"and it returned {_quote(offenders)}. The build has been rolled back, so the "
            "database still holds what it held before."
        )


def _check_queryset(connection: Any, name: str, model: type[Model], violated_by: Q) -> None:
    # The base manager: a filtering default manager would hide rows the rule must see.
    offenders = list(
        model._base_manager.using(connection.alias)
        .filter(violated_by)
        .order_by("pk")
        .values_list("pk", flat=True)[: _SAMPLE + 1]
    )
    if offenders:
        raise InvariantViolated(
            f"The invariant {name!r} was broken by rows of {model._meta.label}. Its violated_by= "
            f"matched the primary keys {_quote(offenders)}. The build has been rolled back, so "
            "the database still holds what it held before."
        )


def _quote(offenders: list[Any]) -> str:
    """The first few offenders, saying so when one more was waiting behind them."""
    shown = ", ".join(repr(row) for row in offenders[:_SAMPLE])
    return f"{shown} and more" if len(offenders) > _SAMPLE else shown
