"""Running declared invariants against the rows that were just loaded."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from django.db.models import Model, Q

from django_data_shape.invariant import Invariant
from django_data_shape.invariant_violated import InvariantViolated

# How many offending rows a failure quotes. Enough to see the shape of the
# problem -- one is a fluke, five is a pattern -- and few enough that a rule
# broken by a million rows still produces a message somebody reads rather than
# scrolls past. One more than this is fetched, so the message can say there
# were more without counting them: a rule broken by every row of a two-million
# row table must not be answered by pulling two million rows back into Python.
_SAMPLE = 5


def check_invariants(connection: Any, invariants: Sequence[Invariant]) -> None:
    """Run every rule, and raise on the first that finds a row.

    Called at the end of :func:`~django_data_shape.build.build`, inside the
    transaction that loaded the rows, so a violation rolls the build back and
    the database is left as it was found. Exported as well as called, because
    the rules are worth running against a database this package did not build:
    a template clone, a restored dump, or the state a suite has worked itself
    into.

    **The first failure stops the run**, rather than every rule being collected
    into one report. A generator that broke one invariant has usually broken
    the rules downstream of it too, and a message listing five consequences of
    one cause is a message that hides the cause.

    Both forms run as SQL. The ``Q`` form goes through ``_base_manager`` --
    never the default manager, which may filter away exactly the rows a rule
    exists to catch -- and reads primary keys so the failure can quote them.
    The ``sql`` form is executed as written, and every row it returns is a
    violation.

    Nothing is guarded here the way generation is. An invariant is *supposed* to
    query: it is the one part of this package whose whole job is a database
    call, which is why the refusal that governs a derivation would be exactly
    backwards.
    """
    for invariant in invariants:
        statement = invariant.sql
        if statement is not None:
            _check_sql(connection, invariant.name, statement)
        else:
            # cast, not a guard: Invariant refuses a declaration with neither
            # spelling and refuses a Q without a model, so both are present
            # here by construction and a branch for their absence would be one
            # no declaration can reach.
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
    # _base_manager, not _default_manager: a manager that filters is the
    # ordinary way a project hides rows, and an invariant that could not see
    # them would report a database that is clean in exactly the place it is not.
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
