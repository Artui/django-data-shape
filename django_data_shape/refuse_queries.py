"""The guard that makes "your code may not call the database" a fact."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any

from django_data_shape.derivation_queried_database import DerivationQueriedDatabase

# Enough of the statement to recognise which query ran, and not so much that a
# generated ``IN`` list with ten thousand parameters becomes the error message.
_SQL_SHOWN = 200


def refuse_queries(
    connection: Any, model: str, derivations: tuple[str, ...]
) -> AbstractContextManager[None]:
    """Refuse any query issued on ``connection`` inside the block.

    Wrapped around **generation**, never around the load. That split is what
    makes the guard usable at all: on PostgreSQL the rows go into
    ``cursor.copy()``, which is not a statement Django wraps, so the whole
    streaming loop can sit inside one guard; on the portable path a chunk is
    generated inside the guard and executed outside it. Either way the only
    thing that can run a query in here is code the caller supplied.

    The message names the model and its derivations rather than the one callable
    at fault. Django hands a wrapper the SQL and not the Python frame that asked
    for it, so naming the exact culprit would mean entering and leaving a
    context manager per row per column -- a cost paid on every build so that a
    rare failure reads slightly better. The statement itself is included
    instead, which in practice identifies the caller immediately.
    """
    return connection.execute_wrapper(_Refuse(model, derivations))


class _Refuse:
    """Django's execute-wrapper contract, with the state it needs to explain itself.

    A class rather than a closure because ``connection.execute_wrappers`` is a
    list of these, and one left behind by an exception is far easier to read
    when its repr says which model was being built.
    """

    def __init__(self, model: str, derivations: tuple[str, ...]) -> None:
        self._model = model
        self._derivations = derivations

    def __call__(self, execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        named = ", ".join(self._derivations) or "none"
        raise DerivationQueriedDatabase(
            f"Generating rows for {self._model} ran a database query, and generation may not: "
            "this package may call your code, but your code may not call the database. A query "
            "per row is the cost COPY exists to avoid, and a hook that may query is a hook that "
            "turns this into a slow fixtures library with extra vocabulary. The statement was "
            f"{sql[:_SQL_SHOWN]!r}. The derivations declared on this table are: {named}; a "
            "distribution or key strategy of your own can reach the database too. Read what you "
            "need before the build and close over it."
        )

    def __repr__(self) -> str:
        return f"_Refuse({self._model!r})"
