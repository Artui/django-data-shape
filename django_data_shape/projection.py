"""A table filled from tables already built, by one statement."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from django.db.models import Field, Model

from django_data_shape.infer_key_strategy import infer_key_strategy
from django_data_shape.invalid_shape import InvalidShape
from django_data_shape.keys.key_strategy import KeyStrategy
from django_data_shape.keys.sql_keys import SqlKeys
from django_data_shape.utils import field_stream, has_db_default, primary_key_field

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
        columns: Sequence[str] | None = None,
        sql: str | None = None,
        params: Sequence[object] = (),
        keys: KeyStrategy | None = None,
    ) -> None:
        self._model = model
        self._per = per
        self._copying = copying
        self._sql = sql
        self._params = tuple(params)
        self._copied: tuple[tuple[str, str, str], ...] = ()
        self._literals: tuple[tuple[str, object], ...] = ()
        self._columns: tuple[str, ...] = ()
        self._join: tuple[str, str] = ("", "")
        self._keys: SqlKeys | None = None

        pk_field = primary_key_field(model)
        if sql is None:
            self._derive(pk_field, columns, keys)
        else:
            self._adopt(pk_field, columns, keys)

    @property
    def model(self) -> type[Model]:
        return self._model

    @property
    def db_table(self) -> str:
        return str(self.model._meta.db_table)

    @property
    def reads(self) -> tuple[type[Model], ...]:
        """The models this projection selects from, or nothing for a raw one.

        What :func:`~django_data_shape.order_tables.order_tables` sorts on. A
        derived projection names its two inputs, so it can be ordered after them
        precisely; a statement this package did not write names nothing, and is
        ordered after everything instead.
        """
        if self._sql is not None:
            return ()
        return (cast("type[Model]", self._per), cast("type[Model]", self._copying))

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
        if self._copying is self._model or self._per is self._model:
            raise InvalidShape(
                f"{self._model.__name__} is projected from itself. A projection fills an empty "
                "table by reading tables already built, so a table that reads itself reads "
                "nothing and fills nothing."
            )
        self._keys = _projectable_keys(self._model, pk_field, keys)
        self._join = _link(self._model, self._per, self._copying)
        self._plan_columns(pk_field)

    def _plan_columns(self, pk_field: Field[Any, Any]) -> None:
        """Decide where every column of the projected table gets its value."""
        per = cast("type[Model]", self._per)
        source = cast("type[Model]", self._copying)
        along = _single_relation(self._model, per)
        available = {field.name: field for field in source._meta.concrete_fields}
        source_pk = _column(primary_key_field(source))

        copied: list[tuple[str, str, str]] = []
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
        self._literals = tuple(literals)
        self._columns = (
            _column(pk_field),
            *(column for column, _alias, _source in copied),
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
                "come out in."
            )
        known = {field.name: field for field in self._model._meta.concrete_fields}
        unknown = sorted(name for name in columns if name not in known)
        if unknown:
            raise InvalidShape(
                f"{self._model.__name__} has no field named {', '.join(unknown)}. "
                f"Its concrete fields are: {', '.join(sorted(known))}."
            )
        if pk_field.name not in columns:
            raise InvalidShape(
                f"{self._model.__name__}.{pk_field.name} is missing from columns=. This package "
                "owns the primary keys, and a projection whose statement it did not write has "
                "to say what they are -- leaving them to the column's sequence would make the "
                "keys depend on a value no declaration mentions."
            )
        self._columns = tuple(_column(known[name]) for name in columns)

    def __repr__(self) -> str:
        if self._sql is not None:
            return f"Projection({self._model.__name__}, columns={self._columns!r}, sql=...)"
        return (
            f"Projection({self._model.__name__}, "
            f"per={cast('type[Model]', self._per).__name__}, "
            f"copying={cast('type[Model]', self._copying).__name__})"
        )


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
            "computes in Python would give it two meanings. Use an integer primary key, or "
            "write the statement with sql= and produce the keys in it."
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


def _link(model: type[Model], per: type[Model], copying: type[Model]) -> tuple[str, str]:
    """The pair of columns ``per`` and ``copying`` are joined on.

    A model they both reach in one step: ``Event`` and ``TemplateSession`` both
    have a foreign key to ``Template``, so the join is on those two columns.
    ``per``'s own primary key counts as a step of length zero, which is what
    makes a source pointing straight at ``per`` the same case rather than a
    special one.
    """
    reachable: list[tuple[type[Model], str]] = [(per, _column(primary_key_field(per)))]
    reachable.extend(
        (cast("type[Model]", field.related_model), _column(field))
        for field in per._meta.concrete_fields
        if field.is_relation
    )
    pairs = sorted(
        (per_column, _column(source_field), target.__name__)
        for target, per_column in reachable
        for source_field in copying._meta.concrete_fields
        if source_field.is_relation and source_field.related_model is target
    )
    if not pairs:
        raise InvalidShape(
            f"{model.__name__} is projected per {per.__name__} copying {copying.__name__}, but "
            f"this package cannot see how those two are joined: nothing {per.__name__} points "
            f"at -- itself included -- is also pointed at by {copying.__name__}. A projection "
            "copies a collection along an edge both sides share. Write the statement with sql= "
            "if the join is not one edge wide."
        )
    if len(pairs) > 1:
        through = ", ".join(sorted({target for _per, _source, target in pairs}))
        raise InvalidShape(
            f"{model.__name__} is projected per {per.__name__} copying {copying.__name__}, and "
            f"they are joinable through more than one model ({through}), so which collection is "
            "being copied is ambiguous. Write the statement with sql= and say which."
        )
    per_column, source_column, _target = pairs[0]
    return per_column, source_column
