from __future__ import annotations

from django.db.models import Model, Q

from django_data_shape.declaration.invalid_shape import InvalidShape


class Invariant:
    """A rule that must hold once the rows are in, checked after the load.

    The second of the three nets, and the only one covering rules the database does not enforce."""

    def __init__(
        self,
        name: str,
        model: type[Model] | None = None,
        *,
        violated_by: Q | None = None,
        sql: str | None = None,
    ) -> None:
        if (violated_by is None) == (sql is None):
            raise InvalidShape(
                f"Invariant {name!r} needs exactly one of violated_by= and sql=, and was given "
                + ("both" if sql is not None else "neither")
                + ". A rule with two spellings is a rule whose reader has to guess which ran."
            )
        if violated_by is not None and model is None:
            raise InvalidShape(
                f"Invariant {name!r} says which rows are wrong with a Q, so it has to say which "
                "model they are rows of."
            )
        if sql is not None and model is not None:
            raise InvalidShape(
                f"Invariant {name!r} is a SQL statement, which names its own tables, so the "
                f"model {model.__name__} it was also given would be read by nothing."
            )
        self._name = name
        self._model = model
        self._violated_by = violated_by
        self._sql = sql

    @property
    def name(self) -> str:
        """What this rule is called, and what a failure reports."""
        return self._name

    @property
    def model(self) -> type[Model] | None:
        """The model ``violated_by`` filters, or None for the SQL form."""
        return self._model

    @property
    def violated_by(self) -> Q | None:
        """The rows that are wrong, or None for the SQL form."""
        return self._violated_by

    @property
    def sql(self) -> str | None:
        """The statement whose every row is a violation, or None for the Q form."""
        return self._sql

    def canonical(self) -> object:
        """The name, the model and the rule. See ``Canonical``."""
        return (
            self._name,
            None if self._model is None else str(self._model._meta.label),
            None if self._violated_by is None else str(self._violated_by),
            self._sql,
        )

    def __repr__(self) -> str:
        rule = (
            f"sql={self._sql!r}" if self._sql is not None else f"violated_by={self._violated_by!r}"
        )
        model = "" if self._model is None else f", {self._model.__name__}"
        return f"Invariant({self._name!r}{model}, {rule})"
