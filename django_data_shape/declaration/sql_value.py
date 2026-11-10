from __future__ import annotations

from django_data_shape.declaration.invalid_shape import InvalidShape

_PER_PLACEHOLDER = "{per}"
_SOURCE_PLACEHOLDER = "{source}"


class SqlValue:
    """One projected column's value, as SQL over the join the projection derives.

    A projection copies a column from the source it names, or takes the model's own default."""

    def __init__(self, expression: str) -> None:
        if not isinstance(expression, str) or not expression.strip():
            raise InvalidShape(
                f"SqlValue needs a SQL expression, got {expression!r}. An empty one would "
                "produce a statement the database refuses, at a point far from the "
                "declaration that caused it."
            )
        self._expression = expression

    @property
    def expression(self) -> str:
        return self._expression

    def render(self, per_alias: str, source_alias: str) -> str:
        """The expression with the join's aliases substituted and ``%`` escaped."""
        return (
            self._expression.replace(_PER_PLACEHOLDER, per_alias)
            .replace(_SOURCE_PLACEHOLDER, source_alias)
            .replace("%", "%%")
        )

    def canonical(self) -> object:
        """The expression itself, which is what decides the rows. See ``Canonical``."""
        return ("SqlValue", self._expression)

    def __repr__(self) -> str:
        return f"SqlValue({self._expression!r})"


__all__ = ["SqlValue"]
