"""A business rule the loaded data is checked against."""

from __future__ import annotations

from django.db.models import Model, Q

from django_data_shape.invalid_shape import InvalidShape


class Invariant:
    """A rule that must hold once the rows are in, checked after the load.

    The second of the three nets, and the only one that covers rules the
    database does not enforce -- which is most of them. A partial
    ``UniqueConstraint`` is refused by PostgreSQL and pre-checked here; a
    denormalised total, a tenant id that must match its parent's, an interval
    chain with no gaps and a status history whose transitions are legal are all
    rules a schema states nowhere and a generator can still break.

    Two ways to say one, and they are mutually exclusive because a declaration
    that carried both would leave the reader to work out which one ran.

    ```python
    Invariant(
        "a project's company matches its board's",
        Project,
        violated_by=~Q(company=F("board__company")),
    )

    Invariant(
        "no company has two active projects",
        sql=\"\"\"
            SELECT company_id, count(*) FROM testapp_project
            WHERE status = 'ACTIVE' GROUP BY company_id HAVING count(*) > 1
        \"\"\",
    )
    ```

    ``violated_by`` is a ``Q`` describing the rows that are **wrong**, not the
    rows that are right. Stated that way round on purpose: the negation of a
    rule is what a failure has to report, so writing the rule positively would
    mean this package inverting a ``Q`` it did not write and reporting rows it
    inferred. The queryset runs through ``_base_manager``, for the reason a
    fan-out reads through it -- a project's default manager may hide exactly the
    rows an invariant exists to catch.

    ``sql`` is the escape hatch, and it is a full statement rather than a
    predicate because the interesting rules are aggregates: *no group has more
    than one*, *these two sums agree*, *this chain has no gap*. **Every row it
    returns is a violation.** A statement returning nothing passes. Nothing here
    parses it, so it may read any table in the database, including ones this
    shape does not build.

    An invariant runs inside the build's own transaction and a violation rolls
    the whole build back -- see
    :class:`~django_data_shape.invariant_violated.InvariantViolated` for why
    that is a build failure rather than a test failure.

    **An invariant changes no row and is still part of a shape's cache key.**
    It has to be: the check runs during the build, so a shape that reused
    another shape's template database would never run it, and a rule that
    silently does not run is worse than no rule -- it is a rule everybody
    believes.
    """

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
        """The name, the model and the rule. See ``Canonical``.

        A ``Q`` is rendered with ``str`` rather than fed in piece by piece. It
        is a tree of tuples that the digest could walk, but ``str`` already
        renders every child in declaration order and is what a reader would
        compare two of them by -- and unlike a callable, it is a faithful
        rendering: two ``Q``s printing alike filter alike.
        """
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
