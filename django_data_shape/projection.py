"""A table filled from tables already built, by one statement."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from copy import copy
from types import MappingProxyType
from typing import Any, cast

from django.db.models import Field, Model

from django_data_shape.infer_key_strategy import infer_key_strategy
from django_data_shape.invalid_shape import InvalidShape
from django_data_shape.keys.key_strategy import KeyStrategy
from django_data_shape.keys.sql_keys import SqlKeys
from django_data_shape.sql_value import SqlValue
from django_data_shape.utils import (
    check_not_inherited,
    check_statistics_target,
    field_stream,
    has_db_default,
    primary_key_field,
)

# The two table aliases the derived statement uses. Named rather than inlined
# because they appear in the join, the select list and the ordering, and a
# reader tracing a generated statement should be able to grep for one word.
_PER = "per"
_SOURCE = "src"


class Projection:
    """One row per pair, copied along a join, by ``INSERT ... SELECT``.

    The shape it exists for is a collection copied across an edge. A
    ``Template`` has ``TemplateSession`` rows; an ``Event`` is created *from* a
    template, and its ``EventSession`` rows mirror that template's sessions::

        Shape(
            Table(Template, rows=500, name=Constant("t")),
            Table(TemplateSession, rows=4_000, template=FanOut(Zipf()), title=Constant("s")),
            Table(Event, rows=200_000, template=FanOut(Zipf()), name=Constant("e")),
            Projection(EventSession, per=Event, copying=TemplateSession),
        )

    That reads as *one ``EventSession`` per ``Event``, copying
    ``TemplateSession``*, and it is the whole declaration: the join, the column
    list and the keys are all derived from the model graph.

    **Why a projection rather than a vocabulary for mirroring.** Three reasons,
    and the first is the one that decides it.

    - It is what the real system already collapses into at scale. One event
      built from a template is a service call; a million of them is one
      statement. A projection *is* that statement, so the test database is built
      the way the production table would be backfilled rather than the way one
      row is created.
    - It needs no new distribution machinery at all. A mirroring mode on
      ``FanOut`` would need an inverted fan-out, a derived cardinality and a way
      to say "as many as over there" -- three vocabulary items to express
      something that is not a distribution in the first place.
    - It reproduces a correlation PostgreSQL cannot see. Sessions-per-event is
      correlated with the template, so every event built from a big template has
      many sessions. A plain ``FanOut`` on ``EventSession.event`` draws that
      count independently and hands the planner a join selectivity real data
      never has -- which is the cross-table correlation this package exists to
      make reproducible.

    **The cardinality is determined, not declared, and that is why this is not a
    ``Table``.** There is no ``rows=`` here: the row count is
    ``count(Event JOIN TemplateSession)``, decided by the tables already built.
    Declaring it as well would be the over-determination this package refuses
    everywhere else -- ``rows=`` on both sides of a one-to-one, an edge count
    beside both marginals of a many-to-many, a status skew beside the fan-out
    that already fixes it. The count comes back in the
    :class:`~django_data_shape.build_result.BuildResult` like every other
    table's, which is what that type was built to report.

    **The join is derived from the model graph.** ``per`` and ``copying`` are
    joined through a model they both reach in one step -- here both have a
    foreign key to ``Template`` -- and ``per``'s own primary key counts as a
    step of length zero, so a source that points straight at ``per`` works the
    same way. Exactly one such link has to exist; zero and several are both
    refused by name, because guessing which edge was meant would build a
    different database from the one that was declared.

    **The columns are derived too**, in this order, for every column of the
    projected model except its primary key:

    - the foreign key to ``per`` gets that row's key;
    - a foreign key to ``copying`` gets the copied row's key, which is how a
      projected collection records where it came from;
    - a column whose **name** matches one on ``copying`` is copied from it. The
      match is by name, so the two columns have to hold compatible types and the
      database says so if they do not;
    - a column with a plain (non-callable) Django ``default=`` gets that value
      as a bound parameter, for the same reason
      :class:`~django_data_shape.table.Table` fills one: a Django default is
      applied by ``save()`` and is not DDL, so a column left out would fail its
      not-null check rather than quietly take it;
    - a nullable column, or one with a real ``db_default``, is left out of the
      statement;
    - anything else is refused by name, pointing at ``sql=``.

    **Where the rows land physically is decided rather than defaulted.** The
    statement orders by ``per``'s key and then the copied row's, which is both
    deterministic -- two builds of one shape have to agree -- and the honest
    physical layout. There is deliberately no ``placement=`` here, unlike on
    :class:`~django_data_shape.fan_out.FanOut`, and the reason is a real
    difference rather than an omission: a fan-out's children arrive over time,
    interleaved, so grouping them is a lie. A copied collection is written in
    one transaction per parent, so grouped *is* arrival order, and the two
    orders a fan-out has to choose between are the same order here.

    **The keys come from the same place as every other table's.** The strategy
    on this declaration decides them, exactly as it does for a ``Table``. It
    just has to be able to say itself in SQL -- see
    :class:`~django_data_shape.keys.sql_keys.SqlKeys` -- because there is no
    declared row count to enumerate in Python and the rows never pass through
    it. Today that means an integer key; a UUID or a caller's own function is
    refused by name at declaration time rather than approximated with a
    different hash, which would give one strategy two meanings depending on
    which statement filled the table.

    **``sql=`` is the escape hatch, and it is the whole statement's ``SELECT``.**
    For anything shaped oddly -- a filter, an aggregate, a three-way join, a
    window -- pass the columns and the select that fills them::

        Projection(
            EventSession,
            columns=("id", "event", "title"),
            sql=(
                "SELECT row_number() OVER (ORDER BY e.id), e.id, t.title "
                "FROM event e JOIN templatesession t ON t.template_id = e.template_id "
                "WHERE t.title <> %s"
            ),
            params=("hidden",),
        )

    The columns are field names and are checked against the model, so a typo is
    refused here rather than by the database. The primary key has to be among
    them: this package owns the keys, and a projection whose statement the
    caller wrote has to say what they are rather than leave them to a sequence
    whose current value is not part of any declaration. Nothing else about the
    select is inspected -- that is what an escape hatch is -- but the build
    still gives it the emptiness check, the sequence reset, the ``ANALYZE`` and
    the transaction.

    **``reads=`` is how such a statement says what it selects from**, and it
    goes with ``sql=`` rather than being an alternative to it. Nothing here
    parses SQL, so a raw statement is opaque and
    :func:`~django_data_shape.order_tables.order_tables` runs it as late as the
    rest of the declaration allows. That is right until something fans out over
    this table: the projection then has to run *before* that table, and may find
    the tables it selects from still empty. Naming them puts it back in the
    graph precisely -- after what it reads, before what reads it -- and it is
    part of the cache key, because a statement run before and after a table
    returns different rows. A derived projection has no use for it: ``per`` and
    ``copying`` already are the answer.

    Like :class:`~django_data_shape.shape.Shape` and
    :class:`~django_data_shape.table.Table`, this is inert data: every attribute
    is read-only and every derived plan is a tuple, so a declaration stays
    hashable and serialisable for the template-database cache that will key on
    it.
    """

    def __init__(
        self,
        model: type[Model],
        *,
        per: type[Model] | None = None,
        copying: type[Model] | None = None,
        through: type[Model] | None = None,
        columns: Sequence[str] | None = None,
        sql: str | None = None,
        params: Sequence[object] = (),
        reads: Sequence[type[Model]] = (),
        keys: KeyStrategy | None = None,
        statistics: Mapping[str, int] | None = None,
        max_rows: int | None = None,
        values: Mapping[str, SqlValue] | None = None,
    ) -> None:
        self._model = model
        self._per = per
        self._copying = copying
        self._through = through
        self._sql = sql
        self._params = tuple(params)
        self._reads: tuple[type[Model], ...] = tuple(reads)
        self._statistics = dict(statistics or {})
        # A bool is an int as far as isinstance is concerned, and a ceiling of
        # one row is never what ``max_rows=True`` meant. Refused here rather
        # than compared later, where it would silently be a very low ceiling.
        if max_rows is not None and (isinstance(max_rows, bool) or max_rows < 1):
            raise InvalidShape(
                f"{model.__name__} declares max_rows={max_rows!r}. A ceiling is a whole "
                "number of rows and at least one, because a projection that inserts "
                "nothing is already refused."
            )
        self._max_rows = max_rows
        self._values = dict(values or {})
        self._expressions: tuple[tuple[str, SqlValue], ...] = ()
        self._copied: tuple[tuple[str, str, str], ...] = ()
        self._literals: tuple[tuple[str, object], ...] = ()
        self._columns: tuple[str, ...] = ()
        self._join: tuple[str, str] = ("", "")
        self._keys: SqlKeys | None = None

        # Before anything reads the model's fields, for the reason ``Table``
        # runs it first: under multi-table inheritance ``_meta.concrete_fields``
        # spans two tables and every column decision below would be made about
        # the wrong one.
        if self._values and sql is not None:
            # `sql=` is the whole SELECT, so there is nowhere for an expression
            # to be put and no join for it to be written over. Two ways of
            # saying one column is the over-determination refused everywhere
            # else in this package.
            raise InvalidShape(
                f"{model.__name__} declares values= and sql= together. `sql=` is the entire "
                "SELECT the insert reads from, so an expression has nowhere to go -- write it "
                "into that statement instead."
            )
        check_not_inherited(model)
        pk_field = primary_key_field(model)
        if sql is None:
            self._derive(pk_field, columns, keys)
        else:
            self._adopt(pk_field, columns, keys)
        # Last, because it is the only check that reads the column list rather
        # than the declaration: both routes above decide which columns the
        # statement writes, and a target on a column nothing writes is a target
        # on nothing.
        self._check_statistics()

    @property
    def model(self) -> type[Model]:
        return self._model

    @property
    def db_table(self) -> str:
        return str(self.model._meta.db_table)

    @property
    def through(self) -> type[Model] | None:
        """The model the derived join runs on, where the caller had to say."""
        return self._through

    @property
    def statistics(self) -> Mapping[str, int]:
        """The per-column statistics targets this projection asks the planner for.

        A projected table needs them for exactly the reason a loaded one does. A
        collection copied along a join carries the source's skew into a second
        table, and the planner records that skew only if the column's target can
        hold it -- the route the rows took in has nothing to do with it.
        """
        return MappingProxyType(self._statistics)

    @property
    def reads(self) -> tuple[type[Model], ...]:
        """The models this projection selects from, or nothing if it did not say.

        What :func:`~django_data_shape.order_tables.order_tables` sorts on. A
        derived projection names its two inputs, so it can be ordered after them
        precisely. A statement this package did not write is opaque -- nothing
        here parses SQL -- so a raw projection answers with whatever ``reads=``
        declared, and with nothing at all when it declared nothing.
        """
        if self._sql is not None:
            return self._reads
        return (cast("type[Model]", self._per), cast("type[Model]", self._copying))

    @property
    def max_rows(self) -> int | None:
        """The largest number of rows this declaration is willing to insert.

        ``None``, and no count is taken -- a declaration that does not ask is
        not charged for the answer.

        It exists because a projection is the one declaration with no ``rows=``,
        and that is deliberate: its cardinality comes from the join, which is
        what reproduces a correlation a ``FanOut`` on the child would destroy.
        The consequence is that the largest table in a database can be the one
        nobody declared a size for. **Its size is a product**, so when both
        sides of the join fan out over the same parents the busy parents
        multiply: raise either declared count by four and the projection grows
        by sixteen. A consumer measured 2,413,223 rows against a declaration
        whose largest number was 300,000.

        There is no default ceiling and there will not be one. How many rows is
        too many is a judgement about size, which this package does not make on
        a caller's behalf anywhere else either -- see the statistics target,
        declared for the same reason. What it can do is act on the caller's own
        number, before the expensive statement rather than after it.
        """
        return self._max_rows

    def statement(self, connection: Any, seed: int) -> tuple[str, tuple[object, ...]]:
        """The ``INSERT ... SELECT`` this declaration means, and its parameters.

        Rendered here rather than stored, because quoting belongs to the
        connection and a declaration must not hold one. Everything the statement
        is made of was decided when the declaration was validated; this only
        spells it.

        A model default lands as a bound parameter rather than as a literal in
        the SQL, so a string default carrying a quote is the driver's problem
        and not this package's.
        """
        quote = connection.ops.quote_name
        columns = ", ".join(quote(column) for column in self._columns)
        into = f"INSERT INTO {quote(self.db_table)} ({columns})"
        if self._sql is not None:
            return f"{into} {self._sql}", self._params
        defaults = tuple(value for _column, value in self._literals)
        return f"{into} {self._select(quote, seed)}", defaults

    def scaled(self, factor: int) -> Projection:
        """This declaration at another size, which for a projection is its ceiling.

        A projection has no row count to multiply -- its size is
        ``count(per JOIN copying)``, so scaling the tables it reads scales it
        without anything being said. ``max_rows`` is different: it is a declared
        number in the same units as that size, and a ceiling that does not move
        with the factor fires on the first growth assertion.

        **Multiplying it by the factor is the right arithmetic rather than an
        approximation of one**, and for the same reason the count needs no
        factor. ``scaled_shape`` scales *every* table, parents included, so a
        parent has the same number of children at every factor; the projection
        is then a sum over ``factor`` times as many parents of an unchanged
        per-parent product, which is ``factor`` times the original.

        Returns ``self`` when there is no ceiling, so a declaration that did not
        ask for one keeps the identity it always had here.
        """
        if self._max_rows is None:
            return self
        scaled = copy(self)
        scaled._max_rows = self._max_rows * factor
        return scaled

    def count_statement(self, connection: Any) -> tuple[str, tuple[object, ...]]:
        """How many rows the insert would write, without writing them.

        Exact rather than estimated: it counts the same join the insert selects
        from, and the select is one row per joined pair, so the two agree by
        construction. The derived form counts the join directly rather than
        wrapping the select -- the window function and the ordering decide what
        the rows *are* and cost real time, while the question here is only how
        many. A ``sql=`` projection is wrapped instead, because this package
        cannot know what the caller's statement is one row per.
        """
        quote = connection.ops.quote_name
        if self._sql is not None:
            return f"SELECT count(*) FROM ({self._sql}) AS {quote('counted')}", self._params
        per = cast("type[Model]", self._per)
        source = cast("type[Model]", self._copying)
        per_alias, source_alias = quote(_PER), quote(_SOURCE)
        per_link, source_link = self._join
        return (
            f"SELECT count(*) "
            f"FROM {quote(per._meta.db_table)} AS {per_alias} "
            f"INNER JOIN {quote(source._meta.db_table)} AS {source_alias} "
            f"ON {source_alias}.{quote(source_link)} = {per_alias}.{quote(per_link)}",
            (),
        )

    def _select(self, quote: Any, seed: int) -> str:
        """The derived select: the key, the copied columns, then the join."""
        per = cast("type[Model]", self._per)
        source = cast("type[Model]", self._copying)
        per_alias, source_alias = quote(_PER), quote(_SOURCE)
        order = (
            f"{per_alias}.{quote(_column(primary_key_field(per)))}, "
            f"{source_alias}.{quote(_column(primary_key_field(source)))}"
        )
        # The window's own ORDER BY is what makes the key deterministic; the
        # outer one is what decides where the rows physically land. They are the
        # same ordering and are written twice because they answer different
        # questions -- collapsing them would make the physical layout depend on
        # how the planner chose to feed the window.
        row = f"row_number() OVER (ORDER BY {order}) - 1"
        key = cast("SqlKeys", self._keys).key_sql(field_stream(seed, self.db_table, ":key"), row)
        values = [key]
        values.extend(f"{quote(alias)}.{quote(column)}" for _target, alias, column in self._copied)
        # Between the copied columns and the bound literals, matching the order
        # `_columns` was built in -- the two lists are one statement and a
        # disagreement between them would write every column into the wrong slot.
        values.extend(
            expression.render(quote(_PER), quote(_SOURCE))
            for _target, expression in self._expressions
        )
        values.extend("%s" for _column, _value in self._literals)
        per_link, source_link = self._join
        return (
            f"SELECT {', '.join(values)} "
            f"FROM {quote(per._meta.db_table)} AS {per_alias} "
            f"INNER JOIN {quote(source._meta.db_table)} AS {source_alias} "
            f"ON {source_alias}.{quote(source_link)} = {per_alias}.{quote(per_link)} "
            f"ORDER BY {order}"
        )

    def _derive(
        self, pk_field: Field[Any, Any], columns: Sequence[str] | None, keys: KeyStrategy | None
    ) -> None:
        """Work the whole statement out of the model graph, or refuse."""
        if self._per is None or self._copying is None:
            raise InvalidShape(
                f"{self._model.__name__} is projected, so it needs either per= and copying= -- "
                "the table it makes one row for, and the collection it copies -- or sql= and "
                "columns= for a statement of your own. It was given neither."
            )
        if columns is not None:
            raise InvalidShape(
                f"{self._model.__name__} declares columns= without sql=. The columns of a "
                "derived projection come from the model graph, so naming them here would say "
                "nothing this package does not already know; columns= exists to describe a "
                "select it did not write."
            )
        if self._params:
            raise InvalidShape(
                f"{self._model.__name__} declares params= without sql=. Parameters belong to a "
                "statement, and this projection's statement is derived rather than given."
            )
        if self._reads:
            raise InvalidShape(
                f"{self._model.__name__} declares reads= without sql=. A derived projection "
                "already names everything it selects from -- per= and copying= are the two -- so "
                "reads= would be a second, quieter answer to a question already answered. It "
                "exists for a statement this package cannot read."
            )
        if self._copying is self._model or self._per is self._model:
            raise InvalidShape(
                f"{self._model.__name__} is projected from itself. A projection fills an empty "
                "table by reading tables already built, so a table that reads itself reads "
                "nothing and fills nothing."
            )
        self._keys = _projectable_keys(self._model, pk_field, keys)
        self._join = _link(self._model, self._per, self._copying, self._through)
        self._plan_columns(pk_field)

    def _plan_columns(self, pk_field: Field[Any, Any]) -> None:
        """Decide where every column of the projected table gets its value."""
        per = cast("type[Model]", self._per)
        source = cast("type[Model]", self._copying)
        along = _single_relation(self._model, per)
        available = {field.name: field for field in source._meta.concrete_fields}
        source_pk = _column(primary_key_field(source))

        copied: list[tuple[str, str, str]] = []
        expressions: list[tuple[str, SqlValue]] = []
        literals: list[tuple[str, object]] = []
        callables: list[str] = []
        missing: list[str] = []
        # Sorted by name for the same reason ``Table.columns()`` is: the order
        # decides the generated statement, and a declaration whose SQL changes
        # when two unrelated model fields are reordered would hash to a
        # different cache key for no reason at all.
        for field in sorted(self._model._meta.concrete_fields, key=lambda field: field.name):
            if field.primary_key:
                continue
            if field is along:
                copied.append((_column(field), _PER, _column(primary_key_field(per))))
            elif field.is_relation and field.related_model is source:
                copied.append((_column(field), _SOURCE, source_pk))
            elif field.name in self._values:
                expressions.append((_column(field), self._values[field.name]))
            elif field.name in available:
                copied.append((_column(field), _SOURCE, _column(available[field.name])))
            elif field.has_default() and callable(field.default):
                callables.append(field.name)
            elif field.has_default():
                literals.append((_column(field), field.get_default()))
            elif field.null or has_db_default(field):
                continue
            else:
                missing.append(field.name)

        declared = {field.name for field in self._model._meta.concrete_fields}
        unknown = sorted(set(self._values) - declared)
        if unknown:
            raise InvalidShape(
                f"{self._model.__name__} has no column(s) {', '.join(unknown)}, named in "
                f"values=. Its columns are: {', '.join(sorted(declared))}."
            )
        answered = sorted(name for name in self._values if name in available)
        if answered:
            raise InvalidShape(
                f"{self._model.__name__}.{', '.join(answered)} is named in values= and is also "
                f"a column {source.__name__} carries under the same name, so the declaration "
                "says two things about one column. Rename it, or drop it from values= and let "
                "it be copied."
            )
        if callables:
            raise InvalidShape(
                f"{self._model.__name__}.{', '.join(callables)} has a callable default, which "
                "this package will not call on your behalf, and a projection could not call it "
                "per row anyway: the rows are made by one statement and never pass through "
                f"Python. Copy the column from {source.__name__} by giving it the same name, or "
                "write the statement with sql=."
            )
        if missing:
            raise InvalidShape(
                f"{self._model.__name__}.{', '.join(missing)} cannot be null, has no default, "
                f"and is not a column {source.__name__} carries under the same name, so a "
                "projection has nothing to put in it. Give it the source's name if it is the "
                "same column, or write the statement with sql=."
            )

        self._copied = tuple(copied)
        self._expressions = tuple(expressions)
        self._literals = tuple(literals)
        self._columns = (
            _column(pk_field),
            *(column for column, _alias, _source in copied),
            *(column for column, _expression in expressions),
            *(column for column, _value in literals),
        )

    def _adopt(
        self, pk_field: Field[Any, Any], columns: Sequence[str] | None, keys: KeyStrategy | None
    ) -> None:
        """Take the caller's own select, checking only what is cheap to check."""
        if self._per is not None or self._copying is not None:
            raise InvalidShape(
                f"{self._model.__name__} declares sql= together with per= or copying=. It is one "
                "form or the other: either this package derives the statement from the model "
                "graph, or you supply it."
            )
        if self._through is not None:
            raise InvalidShape(
                f"{self._model.__name__} declares through= together with sql=. through= names "
                "the model a *derived* join runs on, and a statement you wrote says its own "
                "joins -- so one of the two is not being read. Drop through=, or drop sql= and "
                "let the join be derived."
            )
        if keys is not None:
            raise InvalidShape(
                f"{self._model.__name__} declares sql= together with keys=. A key strategy is "
                "what this package would use to write the key column, and here the statement "
                f"writes it: put {pk_field.name} in columns= and produce it in the select."
            )
        if columns is None:
            raise InvalidShape(
                f"{self._model.__name__} declares sql= without columns=. The select's columns "
                "are what the insert lists, and this package will not guess at the order they "
                "come out in. Name them as fields of this model, in the order the select "
                "produces them -- a relation may be spelled either way, so event and event_id "
                "both name the same column."
            )
        if self._model in self._reads:
            raise InvalidShape(
                f"{self._model.__name__} names itself in reads=. A projection fills an empty "
                "table by reading tables already built, so a table that reads itself reads "
                "nothing -- and load order has no answer for a declaration that has to come "
                "after itself."
            )
        known = _addressable(self._model)
        unknown = sorted(name for name in columns if name not in known)
        if unknown:
            raise InvalidShape(
                f"{self._model.__name__} has no field named {', '.join(unknown)}. "
                f"Its concrete fields are: {', '.join(sorted(known))} -- a relation appears "
                "twice there because either spelling names the same column."
            )
        if pk_field.name not in columns:
            raise InvalidShape(
                f"{self._model.__name__}.{pk_field.name} is missing from columns=. This package "
                "owns the primary keys, and a projection whose statement it did not write has "
                "to say what they are -- leaving them to the column's sequence would make the "
                "keys depend on a value no declaration mentions."
            )
        self._columns = tuple(_column(known[name]) for name in columns)

    def _check_statistics(self) -> None:
        """Refuse a statistics target on a column this statement does not write.

        The same rule ``Table`` applies, read off the column list instead of off
        the declared fields: a column the insert leaves out holds NULLs or a
        database default, and a bigger sample of it describes nothing more
        precisely.
        """
        known = _addressable(self._model)
        for name in sorted(self._statistics):
            where = f"{self._model.__name__}.{name}"
            if name not in known:
                raise InvalidShape(
                    f"{self._model.__name__} has no field named {name}, so it cannot be given a "
                    f"statistics target. Its concrete fields are: {', '.join(sorted(known))}."
                )
            if _column(known[name]) not in self._columns:
                raise InvalidShape(
                    f"{where} has a statistics target, but this projection does not write that "
                    "column: it is nullable or database-defaulted, so every projected row would "
                    "hold the same nothing. Copy it from the source by giving it that name, "
                    "write the statement with sql=, or drop it from statistics=."
                )
            check_statistics_target(where, self._statistics[name])

    def canonical(self) -> object:
        """Everything about this projection that decides a row. See ``Canonical``.

        The derived plan rather than only the two models it was derived from:
        the join, the copied columns and the bound defaults are what the
        statement actually does, and they are what changes when a model this
        projection reads grows a field. A digest over ``per`` and ``copying``
        alone would call two different statements the same.
        """
        return (
            str(self.model._meta.label),
            self.db_table,
            None if self._per is None else str(self._per._meta.label),
            None if self._copying is None else str(self._copying._meta.label),
            # The resolved join below already differs between two through
            # models, but this is stated rather than inferred: a digest that
            # depended on two columns happening to be named differently would
            # be one edit away from calling two declarations the same.
            None if self._through is None else str(self._through._meta.label),
            self._columns,
            self._sql,
            self._params,
            # The expressions decide the values in the columns they fill, so two
            # declarations differing only here build different databases and a
            # key that agreed would serve one for the other.
            tuple((column, expression.canonical()) for column, expression in self._expressions),
            # In the key although it writes no column, because it decides load
            # order and load order reaches the data: a raw statement selecting
            # from a table built before it and after it returns different rows.
            tuple(str(model._meta.label) for model in self._reads),
            self._copied,
            self._literals,
            self._join,
            self._keys,
            {name: self._statistics[name] for name in sorted(self._statistics)},
        )

    def __repr__(self) -> str:
        if self._sql is not None:
            return f"Projection({self._model.__name__}, columns={self._columns!r}, sql=...)"
        return (
            f"Projection({self._model.__name__}, "
            f"per={cast('type[Model]', self._per).__name__}, "
            f"copying={cast('type[Model]', self._copying).__name__})"
        )


def _addressable(model: type[Model]) -> dict[str, Field[Any, Any]]:
    """Every concrete field under both names a caller might reasonably use.

    ``name`` and ``attname`` are the same string for everything but a relation,
    where they are ``event`` and ``event_id``. Both are accepted because the
    refusal that sends a reader to ``columns=`` says "the select's columns are
    what the insert lists" -- and what an insert lists for a foreign key is
    ``event_id``. A consumer followed that sentence and was told there is no
    field named ``event_id``, which is a contradiction rather than a
    correction, and the surrounding documentation reinforced the wrong reading.

    ``name`` wins a collision, which needs a model carrying a plain field named
    exactly some relation's ``attname``. Django's own checks reject that
    (``models.E006``), so the tie-break is there to be deterministic rather than
    because it is reachable.
    """
    fields = list(model._meta.concrete_fields)
    known: dict[str, Field[Any, Any]] = {field.attname: field for field in fields}
    known.update({field.name: field for field in fields})
    return known


def _column(field: Field[Any, Any]) -> str:
    """The column one concrete field writes to.

    ``cast`` rather than a guard, because ``column`` is Optional only in the
    stubs: Django fills it in ``set_attributes_from_name`` for every field
    attached to a model, and every field reaching here came out of
    ``_meta.concrete_fields``. Branching on None would add a path no declaration
    can reach, which is how 100% branch coverage stops being achievable
    honestly.
    """
    return cast("str", field.column)


def _projectable_keys(
    model: type[Model], pk_field: Field[Any, Any], keys: KeyStrategy | None
) -> SqlKeys:
    """The key strategy for a projected table, which must have a SQL form."""
    strategy = keys if keys is not None else infer_key_strategy(pk_field)
    if strategy is None:
        raise InvalidShape(
            f"{model.__name__}.{pk_field.name} is a {type(pk_field).__name__} primary key, and "
            "only integer and UUID keys are inferred. Pass keys= with a strategy for it."
        )
    if not isinstance(strategy, SqlKeys):
        raise InvalidShape(
            f"{model.__name__} is projected, and {strategy!r} cannot assign its keys. A "
            "projection has no declared row count -- its cardinality is decided by the join it "
            "copies along -- so the rows never pass through Python and the keys have to be "
            "written by the statement that inserts them. A strategy that can do that implements "
            "key_sql; this one does not, and computing a different value in SQL from the one it "
            "computes in Python would give it two meanings. For a UUID primary key that is "
            "keys=Md5Keys(), whose two halves are the same digest because md5 exists on both "
            "sides -- UuidKeys derives from blake2b, which PostgreSQL has no equivalent for, and "
            "the two draw different keys so one is never quietly the other. An integer primary "
            "key needs nothing declared, and sql= produces the keys in the statement itself."
        )
    return strategy


def _single_relation(model: type[Model], target: type[Model]) -> Field[Any, Any]:
    """The one foreign key from ``model`` to ``target``, or a refusal."""
    found = [
        field
        for field in model._meta.concrete_fields
        if field.is_relation and field.related_model is target
    ]
    if not found:
        raise InvalidShape(
            f"{model.__name__} has no foreign key to {target.__name__}, so a projection making "
            f"one row per {target.__name__} has nothing to point them at. per= names the table "
            "the projected rows hang off, which has to be a relation the projected model "
            "declares."
        )
    if len(found) > 1:
        raise InvalidShape(
            f"{model.__name__} has more than one foreign key to {target.__name__} "
            f"({', '.join(sorted(field.name for field in found))}), so which one a projected row "
            "hangs off is ambiguous. Write the statement with sql= and say which."
        )
    return found[0]


def _link(
    model: type[Model],
    per: type[Model],
    copying: type[Model],
    through: type[Model] | None = None,
) -> tuple[str, str]:
    """The pair of columns ``per`` and ``copying`` are joined on.

    A model they both reach in one step: ``Event`` and ``TemplateSession`` both
    have a foreign key to ``Template``, so the join is on those two columns.
    ``per``'s own primary key counts as a step of length zero, which is what
    makes a source pointing straight at ``per`` the same case rather than a
    special one.
    """
    every = _every(per, copying)
    candidates = list(every)
    if not candidates:
        raise InvalidShape(
            f"{model.__name__} is projected per {per.__name__} copying {copying.__name__}, but "
            f"this package cannot see how those two are joined: nothing {per.__name__} points "
            f"at -- itself included -- is also pointed at by {copying.__name__}. A projection "
            "copies a collection along an edge both sides share. Write the statement with sql= "
            "if the join is not one edge wide."
        )
    if through is not None:
        candidates = [one for one in candidates if one[2] == through.__name__]
        if not candidates:
            offered = ", ".join(sorted({one[2] for one in every}))
            raise InvalidShape(
                f"{model.__name__} declares through={through.__name__}, and {per.__name__} and "
                f"{copying.__name__} are not both joinable through it. What they do share is: "
                f"{offered}."
            )
    if len(candidates) > 1:
        raise InvalidShape(_ambiguous(model, per, copying, candidates))
    per_column, source_column, _target, _per_name, _source_name = candidates[0]
    return per_column, source_column


def _ambiguous(
    model: type[Model],
    per: type[Model],
    copying: type[Model],
    candidates: list[tuple[str, str, str, str, str]],
) -> str:
    """Why the join could not be derived, and which correction actually applies.

    **Two shapes of ambiguity, and they take different corrections**, so telling
    them apart is most of this function. Several *models* means naming one with
    ``through=``. Several *edges to one model* means ``through=`` cannot narrow
    it at all, because both edges satisfy it -- and that is what an abstract
    base produces, since ``created_by`` and ``updated_by`` both reach ``User``.

    A model reached by exactly one edge from each side is the only kind
    ``through=`` can resolve, so only those are offered. Naming the
    alphabetically first candidate instead would have suggested
    ``through=Auditor`` on the schema this was written for, which is the one
    answer that cannot work -- the audit model is reached twice from both
    sides, and is never the collection being copied.
    """
    counts = Counter(one[2] for one in candidates)
    resolvable = sorted(name for name, seen in counts.items() if seen == 1)
    repeated = sorted(name for name, seen in counts.items() if seen > 1)
    # What gets listed depends on what has to be chosen between, and bounding
    # it matters: two audit columns on each side is already four edges through
    # one model, and four would be sixteen. Where several models are candidates,
    # the models are the choice and through= is spelled with one of them.
    # Where only one is, the edges are the choice and through= cannot express
    # it, so those are named in full.
    if len(counts) > 1:
        edges = ", ".join(
            f"{name} ({seen} way{'' if seen == 1 else 's'})"
            for name, seen in sorted(counts.items())
        )
    else:
        edges = ", ".join(
            f"{per.__name__}.{per_name} to {copying.__name__}.{source_name}"
            for _pc, _sc, _target, per_name, source_name in candidates
        )
    if resolvable:
        remedy = (
            f"Name the one you mean with through={resolvable[0]}, or write the statement with sql=."
        )
    else:
        remedy = (
            "through= cannot narrow this: every candidate is reached by more than one edge, so "
            "naming the model leaves the same choice. Write the statement with sql= and say "
            "which pair of columns the join is on."
        )
    if repeated:
        aside = (
            f" {', '.join(repeated)} is reached by more than one edge from each side, which is "
            "what an abstract base carrying created_by/updated_by does to every pair of models "
            "in a schema -- an audit column is never the collection being copied."
        )
    else:
        aside = ""
    return (
        f"{model.__name__} is projected per {per.__name__} copying {copying.__name__}, and this "
        f"package can see {len(candidates)} ways to join them ({edges}), so which collection is "
        f"being copied is ambiguous. {remedy}{aside}"
    )


def _every(per: type[Model], copying: type[Model]) -> list[tuple[str, str, str, str, str]]:
    """Every join this package can see between the two: how, and through what.

    Its own function because two callers need it and they need different halves
    of it. ``_link`` narrows the list to what ``through=`` allows, and a refusal
    has to quote the list *before* that narrowing -- otherwise a caller who
    named the wrong model is told there is nothing, and left to guess both the
    correction and the spelling of it.

    Each entry is the join column on each side, the model it goes through, and
    the two field names, because the field names are what a refusal has to say
    when several edges reach one model and ``through=`` therefore cannot help.
    """
    reachable: list[tuple[type[Model], str, str]] = [
        (per, _column(primary_key_field(per)), primary_key_field(per).name)
    ]
    reachable.extend(
        (cast("type[Model]", field.related_model), _column(field), field.name)
        for field in per._meta.concrete_fields
        if field.is_relation
    )
    return sorted(
        (per_column, _column(source_field), target.__name__, per_name, source_field.name)
        for target, per_column, per_name in reachable
        for source_field in copying._meta.concrete_fields
        if source_field.is_relation and source_field.related_model is target
    )
