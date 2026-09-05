"""A projected column written as an expression the database evaluates."""

from __future__ import annotations

from django_data_shape.invalid_shape import InvalidShape

_PER_PLACEHOLDER = "{per}"
_SOURCE_PLACEHOLDER = "{source}"


class SqlValue:
    """One projected column's value, as SQL over the join the projection derives.

    A projection copies a column from the source it names, or takes the model's
    own default, and those are the only two answers it has. A projected table's
    **measure** column is neither: the score on a review, the amount on a
    generated line, the reading on a sample. It belongs to the projected row and
    to nothing the source carries.

    Leaving it to the model default is legal and is the wrong answer *for this
    package specifically*: one value across every projected row is
    ``n_distinct = 1``, which is the exact shape a planner cannot use. A library
    whose whole purpose is planner realism would then be building a table it had
    made unplannable, and the declaration would look correct.

    ``sql=`` already answers this and answers it expensively: it replaces the
    whole ``SELECT``, so the join stops being derived from the model graph and
    can drift from it afterwards, the copied columns are written out by hand,
    and the key strategy has to be spelled in SQL. ``values=`` gives up none of
    that and writes one expression for the one column that needs one::

        Projection(
            ReviewScore,
            per=Review,
            copying=Criterion,
            values={"score": SqlValue("mod({per}.id * 31 + {source}.id * 17, 5) + 1")},
        )

    ``{per}`` and ``{source}`` are substituted with the aliases the derived
    statement uses, quoted for the connection. They are placeholders rather than
    the aliases themselves because the aliases are this package's private
    business: a declaration that spelled them would break the day they changed,
    and a reader could not tell which side was which.

    **It is SQL rather than a distribution, and that is a decision worth
    stating.** A :class:`~django_data_shape.distributions.distribution.Distribution`
    computes from ``draw(stream, row)``, which is SplitMix64 -- expressible in
    PostgreSQL only through ``numeric`` modular arithmetic and casts across the
    sign boundary, where a single mistake gives one declaration two meanings
    depending on which statement filled the table. That is the divergence
    :class:`~django_data_shape.keys.sql_keys.SqlKeys` exists to refuse, and it
    is not worth buying convenience with. An expression the caller wrote is
    honest about being the caller's.

    The expression is inert data, so a shape holding one still digests.
    """

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
        """The expression with the join's aliases substituted in."""
        return self._expression.replace(_PER_PLACEHOLDER, per_alias).replace(
            _SOURCE_PLACEHOLDER, source_alias
        )

    def canonical(self) -> object:
        """The expression itself, which is what decides the rows. See ``Canonical``."""
        return ("SqlValue", self._expression)

    def __repr__(self) -> str:
        return f"SqlValue({self._expression!r})"


__all__ = ["SqlValue"]
