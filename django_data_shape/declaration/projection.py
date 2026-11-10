from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from copy import copy
from types import MappingProxyType
from typing import Any, cast

from django.db.models import Field, Model

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.declaration.sql_value import SqlValue
from django_data_shape.keys.infer_key_strategy import infer_key_strategy
from django_data_shape.keys.key_strategy import KeyStrategy
from django_data_shape.keys.sql_keys import SqlKeys
from django_data_shape.utils import (
    check_not_inherited,
    check_statistics_target,
    field_stream,
    has_db_default,
    many_to_many_refusal,
    primary_key_field,
)

# The two table aliases the derived statement uses. Named rather than inlined
# because they appear in the join, the select list and the ordering, and a
# reader tracing a generated statement should be able to grep for one word.
_PER = "per"
_SOURCE = "src"

# Every pyformat shape psycopg 3 and Django's SQLite wrapper accept: a doubled
# `%%`, a positional placeholder (psycopg also takes `%b` and `%t`), and a named
# one. Written as what is *allowed* rather than as what is wrong, because the
# set of legal placeholders is short and closed and the set of mistakes is not.
_PLACEHOLDER = re.compile(r"%(?:%|[sbt]|\([^)]*\)[sbt])")

# One `values=` entry naming another: `{values.approved_amount}`. Dotted rather
# than a bare `{approved_amount}` because `{per}` and `{source}` already occupy
# that space, and a model is perfectly entitled to a column called `per`. The
# name is captured loosely -- anything but a brace -- so that a misspelling and
# a nonsense path both arrive at the same refusal, which can list what was
# available instead of arguing about the shape of the name.
_REFERENCE = re.compile(r"\{values\.([^{}]*)\}")


class Projection:
    """One row per pair, copied along a join, by ``INSERT ... SELECT``.

    The shape it exists for is a collection copied across an edge, one row per joined pair."""

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
        # A bool passes as an int, so max_rows=True is refused here rather than read as 1.
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

        if self._values and sql is not None:
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
        """The per-column statistics targets this projection asks the planner for."""
        return MappingProxyType(self._statistics)

    @property
    def reads(self) -> tuple[type[Model], ...]:
        """The models this projection selects from, or nothing if it did not say."""
        if self._sql is not None:
            return self._reads
        return (cast("type[Model]", self._per), cast("type[Model]", self._copying))

    @property
    def max_rows(self) -> int | None:
        """The largest number of rows this declaration is willing to insert."""
        return self._max_rows

    def statement(self, connection: Any, seed: int) -> tuple[str, tuple[object, ...]]:
        """The ``INSERT ... SELECT`` this declaration means, and its parameters."""
        quote = connection.ops.quote_name
        columns = ", ".join(quote(column) for column in self._columns)
        into = f"INSERT INTO {quote(self.db_table)} ({columns})"
        if self._sql is not None:
            return f"{into} {self._sql}", self._params
        defaults = tuple(value for _column, value in self._literals)
        return f"{into} {self._select(quote, seed)}", defaults

    def scaled(self, factor: int) -> Projection:
        """This declaration at another size, which for a projection is its ceiling."""
        if self._max_rows is None:
            return self
        scaled = copy(self)
        scaled._max_rows = self._max_rows * factor
        return scaled

    def count_statement(self, connection: Any) -> tuple[str, tuple[object, ...]]:
        """How many rows the insert would write, without writing them."""
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
        row = f"row_number() OVER (ORDER BY {order}) - 1"
        key = cast("SqlKeys", self._keys).key_sql(field_stream(seed, self.db_table, ":key"), row)
        values = [key]
        values.extend(f"{quote(alias)}.{quote(column)}" for _target, alias, column in self._copied)
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
        expressions: list[tuple[str, str]] = []
        literals: list[tuple[str, object]] = []
        callables: list[str] = []
        missing: list[str] = []
        for field in sorted(self._model._meta.concrete_fields, key=lambda field: field.name):
            if field.primary_key:
                continue
            if field is along:
                copied.append((_column(field), _PER, _column(primary_key_field(per))))
            elif field.is_relation and field.related_model is source:
                copied.append((_column(field), _SOURCE, source_pk))
            elif field.name in self._values:
                expressions.append((_column(field), field.name))
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

        _check_references(self._model, self._values, available)

        self._copied = tuple(copied)
        self._expressions = tuple(
            (column, SqlValue(_resolve_references(self._model, name, self._values, (name,))))
            for column, name in expressions
        )
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
        _refuse_a_lone_percent(self._model, cast("str", self._sql))
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
            if (refusal := many_to_many_refusal(self._model, unknown)) is not None:
                raise InvalidShape(refusal)
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
        """Refuse a statistics target on a column this statement does not write."""
        known = _addressable(self._model)
        for name in sorted(self._statistics):
            where = f"{self._model.__name__}.{name}"
            if name not in known:
                if (refusal := many_to_many_refusal(self._model, [name])) is not None:
                    raise InvalidShape(refusal)
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
        """Everything about this projection that decides a row. See ``Canonical``."""
        return (
            str(self.model._meta.label),
            self.db_table,
            None if self._per is None else str(self._per._meta.label),
            None if self._copying is None else str(self._copying._meta.label),
            None if self._through is None else str(self._through._meta.label),
            self._columns,
            self._sql,
            self._params,
            tuple((column, expression.canonical()) for column, expression in self._expressions),
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


def _check_references(
    model: type[Model],
    values: Mapping[str, SqlValue],
    available: Mapping[str, Field[Any, Any]],
) -> None:
    """Refuse a ``{values.x}`` that names nothing, or a cycle, at declaration time."""
    for name in sorted(values):
        for target in _REFERENCE.findall(values[name].expression):
            if target in values:
                continue
            if target in available:
                raise InvalidShape(
                    f"{model.__name__}.{name} references {{values.{target}}}, and {target} is "
                    f"not written by values= -- it is copied from {target!r} on the source. "
                    f"Write {{source}}.{target} instead, which is the column itself rather "
                    "than an expression for it."
                )
            raise InvalidShape(
                f"{model.__name__}.{name} references {{values.{target}}}, which values= does "
                f"not declare. It declares: {', '.join(sorted(values))}."
            )

    for name in sorted(values):
        _resolve_references(model, name, values, (name,))


def _resolve_references(
    model: type[Model],
    name: str,
    values: Mapping[str, SqlValue],
    stack: tuple[str, ...],
) -> str:
    """``values[name]`` with every ``{values.x}`` replaced by x's own expression."""

    def substitute(match: re.Match[str]) -> str:
        target = match.group(1)
        if target in stack:
            path = " -> ".join((*stack, target))
            raise InvalidShape(
                f"{model.__name__} declares a cycle in values=: {path}. Each expression is "
                "written out in place of the reference to it, so a cycle has no expansion at "
                "all rather than an expensive one."
            )
        return f"({_resolve_references(model, target, values, (*stack, target))})"

    return _REFERENCE.sub(substitute, values[name].expression)


def _refuse_a_lone_percent(model: type[Model], sql: str) -> None:
    """Refuse a ``%`` that is neither a placeholder nor doubled, here rather than at build."""
    position = 0
    while (found := sql.find("%", position)) != -1:
        placeholder = _PLACEHOLDER.match(sql, found)
        if placeholder is None:
            raise InvalidShape(
                f"{model.__name__} declares sql= containing a lone '%' in "
                f"{sql[max(0, found - 12) : found + 8].strip()!r}. The statement is run with "
                "its parameters, so a '%' that is not a placeholder is read as the start of "
                "one and the driver refuses it far from here. Write '%%' for the modulo "
                "operator. (A derived projection's SqlValue needs no escaping, because there "
                "you supply no parameters and the package escapes for you.)"
            )
        position = placeholder.end()


def _addressable(model: type[Model]) -> dict[str, Field[Any, Any]]:
    """Every concrete field under both names a caller might reasonably use."""
    fields = list(model._meta.concrete_fields)
    known: dict[str, Field[Any, Any]] = {field.attname: field for field in fields}
    known.update({field.name: field for field in fields})
    return known


def _column(field: Field[Any, Any]) -> str:
    """The column one concrete field writes to."""
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
    """The pair of columns ``per`` and ``copying`` are joined on."""
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
    """Why the join could not be derived, and which correction actually applies."""
    counts = Counter(one[2] for one in candidates)
    resolvable = sorted(name for name, seen in counts.items() if seen == 1)
    repeated = sorted(name for name, seen in counts.items() if seen > 1)
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
    """Every join this package can see between the two: how, and through what."""
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
