from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any

from django_data_shape.generation.derivation_queried_database import DerivationQueriedDatabase

# Enough of the statement to recognise which query ran, and not so much that a
# generated ``IN`` list with ten thousand parameters becomes the error message.
_SQL_SHOWN = 200


def refuse_queries(
    connection: Any, model: str, derivations: tuple[str, ...]
) -> AbstractContextManager[None]:
    """Refuse any query issued on ``connection`` inside the block."""
    return connection.execute_wrapper(_Refuse(model, derivations))


class _Refuse:
    """Django's execute-wrapper contract, with the state it needs to explain itself.

    A class rather than a closure, since one left behind by an exception is far easier to read."""

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
