"""One model's declared shape."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from django.db.models import Field, Model

from django_data_shape.derivations.derivation import Derivation
from django_data_shape.derivations.per_parent import PerParent
from django_data_shape.derivations.scope import Scope
from django_data_shape.distributions.ascending import Ascending
from django_data_shape.distributions.bounded import Bounded
from django_data_shape.distributions.constant import Constant
from django_data_shape.distributions.distribution import Distribution
from django_data_shape.fan_out import FanOut
from django_data_shape.infer_key_strategy import infer_key_strategy
from django_data_shape.invalid_shape import InvalidShape
from django_data_shape.keys.key_strategy import KeyStrategy
from django_data_shape.order_derivations import order_derivations
from django_data_shape.utils import (
    check_not_inherited,
    check_statistics_target,
    has_db_default,
    primary_key_field,
)


class Table:
    """How many rows of one model, and how each column is distributed.

    Field distributions are given as keyword arguments because that is the form
    a reader scans fastest. ``fields=`` is the escape hatch, and it is not
    optional politeness: a model may legitimately have a column called ``rows``
    or ``model``, and Python's own argument binding would silently hand that
    keyword to this signature instead. Without the mapping form those models
    would simply be undeclarable.

    ``statistics=`` maps a field name to the number of buckets the planner
    should keep for that column::

        Table(Order, rows=2_000_000, status=Skew(weights), statistics={"status": 500})

    A target is a physical property of the column rather than of the
    distribution, which is why it is declared beside the distributions and not
    inside one: the same skew wants a different target in a table of two
    thousand rows and a table of two million, and a caller's own distribution
    would have to grow a parameter it has no use for. PostgreSQL keeps at most
    ``target`` most-common values and ``target`` histogram bounds, and samples
    300 times that many rows, so this is the dial that decides how much of a
    declared shape the planner can actually record. A column left out keeps
    whatever the schema gives it. See
    :func:`~django_data_shape.apply_statistics_targets.apply_statistics_targets`
    for what the build does with it, including the one thing it refuses.

    Every refusal below happens here, at declaration time, rather than during
    the load. A shape that cannot describe a database should say so before it
    has spent a minute generating rows, and the message should name the field --
    a reader who has to re-derive which column was meant has been given an error
    that knows more than it says.
    """

    def __init__(
        self,
        model: type[Model],
        rows: int,
        fields: Mapping[str, Distribution | FanOut | Derivation] | None = None,
        keys: KeyStrategy | None = None,
        statistics: Mapping[str, int] | None = None,
        **field_distributions: Distribution | FanOut | Derivation,
    ) -> None:
        if rows < 0:
            raise InvalidShape(f"{model.__name__} cannot have {rows} rows.")

        declared: dict[str, Distribution | FanOut | Derivation] = dict(fields or {})
        overlap = sorted(set(declared) & set(field_distributions))
        if overlap:
            raise InvalidShape(
                f"{model.__name__} declares {', '.join(overlap)} twice, once in fields= and "
                "once as a keyword. Use one or the other."
            )
        declared.update(field_distributions)

        self._model = model
        self._rows = rows
        self._fields = declared
        self._keys = keys
        self._statistics = dict(statistics or {})
        self._computation_order: tuple[str, ...] = ()
        self._validate()

    # Read-only, because every rule in this class is enforced once, in
    # __init__. Leaving the attributes writable meant a declaration could be
    # edited afterwards into one that would have been refused -- and silently,
    # since nothing re-runs the checks. A shape also has to stay hashable data
    # for the template cache that comes later, which a mutable one cannot be.
    @property
    def model(self) -> type[Model]:
        return self._model

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def keys(self) -> KeyStrategy:
        """How this table's primary keys are decided.

        Never None by the time anyone can read it: _validate either inferred a
        strategy from the primary key's type or refused the declaration.
        """
        return cast("KeyStrategy", self._keys)

    @property
    def fields(self) -> Mapping[str, Distribution | FanOut | Derivation]:
        """The declared distributions, including defaults filled in from the model."""
        return MappingProxyType(self._fields)

    @property
    def statistics(self) -> Mapping[str, int]:
        """The per-column statistics targets this table asks the planner for.

        Empty unless the declaration said otherwise, which means the columns
        keep whatever target the schema gives them -- ``default_statistics_target``
        for a column no migration has touched.
        """
        return MappingProxyType(self._statistics)

    @property
    def db_table(self) -> str:
        return str(self.model._meta.db_table)

    def columns(self) -> tuple[tuple[str, Field[Any, Any]], ...]:
        """The declared fields, in a stable order, with their model fields.

        Sorted by name rather than left in declaration order: the order decides
        the column list of the ``COPY`` statement, and a shape whose generated
        SQL changes when two keyword arguments are swapped would hash to a
        different cache key for no reason.
        """
        meta = self.model._meta
        # cast because get_field's return type covers reverse relations too,
        # which _validate has already ruled out for every name reaching here.
        return tuple(
            (name, cast("Field[Any, Any]", meta.get_field(name))) for name in sorted(self.fields)
        )

    def relations(self) -> tuple[tuple[str, Field[Any, Any]], ...]:
        """The declared relation columns, in the same stable order as columns()."""
        meta = self.model._meta
        return tuple(
            (name, cast("Field[Any, Any]", meta.get_field(name)))
            for name in sorted(self.fields)
            if cast("Field[Any, Any]", meta.get_field(name)).is_relation
        )

    def computation_order(self) -> tuple[str, ...]:
        """The declared derivations, dependencies first.

        A second order over the same columns, and deliberately not the one
        ``columns()`` returns. That one is sorted by name to keep the ``COPY``
        statement stable; this one is a topological sort of what depends on
        what. Computed once, when the declaration was validated, because a cycle
        among derivations is a refusal and refusals belong at declaration time.
        """
        return self._computation_order

    def parent_fields(self) -> Mapping[str, tuple[str, ...]]:
        """Which of a parent's columns this table's derivations read, per relation.

        The build turns this into columns on the query that already reads the
        parent's keys, so a child reaches its parent's values through the
        fan-out it already declared rather than through a lookup per row.
        """
        wanted: dict[str, set[str]] = {}
        for declared in self.fields.values():
            if not isinstance(declared, Derivation) or declared.scope is not Scope.PARENT:
                continue
            for source in declared.sources:
                relation, _, field_name = source.partition(".")
                wanted.setdefault(relation, set()).add(field_name)
        return {relation: tuple(sorted(names)) for relation, names in wanted.items()}

    def _validate(self) -> None:
        # First, because every check below reads ``_meta.concrete_fields`` and
        # under multi-table inheritance that list spans two tables while this
        # declaration is about one. Refusing here means the reader is told about
        # the inheritance rather than about whichever column happened to be
        # noticed first somewhere downstream.
        check_not_inherited(self.model)
        meta = self.model._meta
        known = {field.name: field for field in meta.concrete_fields}

        unknown = sorted(name for name in self.fields if name not in known)
        if unknown:
            raise InvalidShape(
                f"{self.model.__name__} has no field named {', '.join(unknown)}. "
                f"Its concrete fields are: {', '.join(sorted(known))}."
            )

        pk_field = primary_key_field(self.model)
        if self._keys is None:
            self._keys = infer_key_strategy(pk_field)
        if self._keys is None:
            raise InvalidShape(
                f"{self.model.__name__}.{pk_field.name} is a {type(pk_field).__name__} primary "
                "key, and only integer and UUID keys are inferred. Pass keys= with a strategy "
                "for it -- KeyFunction takes any deterministic function of the row index. "
                "Inventing values for a key column is how a character primary key once got "
                'loaded with the strings "1", "2" and "3".'
            )

        pk_names = {name for name, field in known.items() if field.primary_key}
        declared_pk = sorted(pk_names & set(self.fields))
        if declared_pk:
            raise InvalidShape(
                f"{self.model.__name__}.{', '.join(declared_pk)} is the primary key, which this "
                "package assigns itself as a dense 1..N range. That is what lets a foreign key "
                "be satisfied without a lookup, so it is not available to declare."
            )

        # A relation takes a FanOut and nothing else. A value distribution over a
        # foreign key column would emit ids drawn from thin air, pointing at rows
        # that may not exist -- the one thing referential integrity by
        # construction exists to make impossible.
        mismatched = sorted(
            name
            for name, declared in self.fields.items()
            if known[name].is_relation and not isinstance(declared, FanOut)
        )
        if mismatched:
            raise InvalidShape(
                f"{self.model.__name__}.{', '.join(mismatched)} is a relation, so it needs a "
                "FanOut rather than a value distribution or a derivation. Either would emit keys "
                "drawn from nothing, pointing at rows that may not exist."
            )
        self_referential = sorted(
            name
            for name, declared in self.fields.items()
            if isinstance(declared, FanOut)
            and known[name].is_relation
            and known[name].related_model is self.model
        )
        if self_referential:
            raise InvalidShape(
                f"{self.model.__name__}.{', '.join(self_referential)} points at its own table, "
                "and a fan-out reads keys from a table that is still empty at load time. "
                "Self-referential trees are their own feature, not a fan-out."
            )
        misapplied = sorted(
            name
            for name, declared in self.fields.items()
            if not known[name].is_relation and isinstance(declared, FanOut)
        )
        if misapplied:
            raise InvalidShape(
                f"{self.model.__name__}.{', '.join(misapplied)} is not a relation, so a FanOut "
                "has nothing to fan out over."
            )

        self._resolve_defaults(known)
        # After the defaults for the reason the derivation check is: a target on
        # a column the model defaults rather than the caller declared is a
        # target on a column this shape does fill, and before this point that
        # column is not in ``fields`` to be recognised.
        self._check_statistics(known)
        self._check_satisfiable(known)
        # After the defaults, not before: a derivation may legitimately read a
        # column the model defaulted rather than the caller declared, and before
        # this point that column is not in ``fields`` to be found.
        self._check_derivations(known)
        self._computation_order = order_derivations(self.model.__name__, self._fields)

    def _check_statistics(self, known: dict[str, Field[Any, Any]]) -> None:
        """Refuse a statistics target on a column this shape does not fill.

        A target is a promise about what the planner will record for a column,
        and it can only be kept for a column that has values in it. The two ways
        to ask for one that cannot be kept are naming a field the model does not
        have, and naming one this shape leaves alone -- a nullable column nobody
        declared holds nothing but NULLs, so a bigger sample of it describes
        nothing more precisely.

        The primary key counts as filled, because this package assigns it.
        """
        for name in sorted(self._statistics):
            where = f"{self.model.__name__}.{name}"
            if name not in known:
                raise InvalidShape(
                    f"{self.model.__name__} has no field named {name}, so it cannot be given a "
                    f"statistics target. Its concrete fields are: {', '.join(sorted(known))}."
                )
            if name not in self.fields and not known[name].primary_key:
                raise InvalidShape(
                    f"{where} has a statistics target, but this shape does not fill that column: "
                    "it is nullable or database-defaulted and was left undeclared, so every row "
                    "would hold the same nothing. Declare a distribution for it, or drop it from "
                    "statistics=."
                )
            check_statistics_target(where, self._statistics[name])

    def _check_derivations(self, known: dict[str, Field[Any, Any]]) -> None:
        """Refuse a derivation whose sources are not there to be read.

        Every source name is resolvable here, at declaration time, and none of
        it needs a connection: a row source is a column of this table, a parent
        source is a column of a model reachable through a declared ``FanOut``,
        and a rank source is a name the declaration invented and so cannot be
        wrong. A source that cannot be resolved would otherwise surface as a
        ``KeyError`` from inside the generator, naming neither the column that
        asked nor the one it wanted.
        """
        for name, declared in sorted(self._fields.items()):
            if not isinstance(declared, Derivation):
                continue
            if declared.scope is Scope.ROW:
                self._check_row_sources(name, declared)
            elif declared.scope is Scope.PARENT:
                self._check_parent_sources(name, declared, known)
            elif declared.scope is Scope.GROUP:
                self._check_group_sources(name, declared)

    def _check_row_sources(self, name: str, declared: Derivation) -> None:
        unknown = sorted(source for source in declared.sources if source not in self._fields)
        if unknown:
            raise InvalidShape(
                f"{self.model.__name__}.{name} is derived from {', '.join(unknown)}, which "
                f"{'are' if len(unknown) > 1 else 'is'} not declared on this table. A row-scoped "
                "source is another column of the same row, so it has to be a column this shape "
                f"fills. Its columns are: {', '.join(sorted(self._fields))}."
            )

    def _check_parent_sources(
        self, name: str, declared: Derivation, known: dict[str, Field[Any, Any]]
    ) -> None:
        for source in declared.sources:
            relation, dot, field_name = source.partition(".")
            if not dot:
                raise InvalidShape(
                    f"{self.model.__name__}.{name} reads {source!r} from a parent, so it has to "
                    "name the relation and the field: 'relation.field'."
                )
            fan_out = self._fields.get(relation)
            if not isinstance(fan_out, FanOut):
                raise InvalidShape(
                    f"{self.model.__name__}.{name} reads {source!r} from a parent, but "
                    f"{relation} is not a fan-out declared on this table. A parent is reached "
                    "through the fan-out that already decides which parent owns the row, so the "
                    "relation has to be declared before anything can be read across it."
                )
            if fan_out.null:
                raise InvalidShape(
                    f"{self.model.__name__}.{name} reads {source!r} from a parent, but "
                    f"{relation} has null={fan_out.null!r}, so some of these rows have no parent "
                    "to read from. Drop the null share, or fill this column from a distribution "
                    "instead."
                )
            parent = cast("type[Model]", known[relation].related_model)
            available = {field.name for field in parent._meta.concrete_fields}
            if field_name not in available:
                raise InvalidShape(
                    f"{self.model.__name__}.{name} reads {source!r}, but {parent.__name__} has no "
                    f"field named {field_name}. Its concrete fields are: "
                    f"{', '.join(sorted(available))}."
                )

    def _check_group_sources(self, name: str, declared: Derivation) -> None:
        """Refuse a group-scoped column whose groups are not there to be grouped by.

        A group is a parent's share of the child range, so the source has to be
        a ``FanOut`` declared on this table -- the same requirement a parent
        source has, and for a sharper reason: a parent source with no fan-out
        has nothing to read, while a group source with no fan-out has no
        *partition*, and it is the partition that makes a per-group rule
        computable at all.

        A null share is refused for a reason that is not the parent case's. A
        child with a NULL foreign key belongs to no group, so no per-group rule
        says anything about it -- and PostgreSQL treats each NULL as distinct in
        a unique index, so those rows are exempt from the very constraint this
        exists to satisfy. Generating them would leave a share of the table
        outside the rule while the declaration read as though it covered
        everything.
        """
        for source in declared.sources:
            fan_out = self._fields.get(source)
            if not isinstance(fan_out, FanOut):
                raise InvalidShape(
                    f"{self.model.__name__}.{name} is decided per group of {source!r}, but "
                    f"{source} is not a fan-out declared on this table. A group is a parent's "
                    "share of the child rows, so the fan-out that partitions them has to be "
                    "declared before anything can be decided per group."
                )
            if fan_out.null:
                raise InvalidShape(
                    f"{self.model.__name__}.{name} is decided per group of {source!r}, but "
                    f"{source} has null={fan_out.null!r}, so some of these rows belong to no "
                    "group at all. PostgreSQL counts each NULL as its own group in a unique "
                    "index, so those rows would sit outside the rule while the declaration read "
                    "as though it covered them. Drop the null share."
                )
        if isinstance(declared, PerParent) and declared.order_by is not None:
            self._check_group_order(name, declared)

    def _check_group_order(self, name: str, declared: PerParent) -> None:
        """Refuse an ``order_by`` this package cannot make true.

        ``order_by`` claims that the last row of a group *under that column's
        ordering* is the last row of the group *as the fan-out partitioned it*.
        It is checked rather than performed, because performing it would mean
        sorting a group, and holding a group is the one thing streaming into
        ``COPY`` cannot do.

        Two conditions make the claim true, and both are decidable here.

        The column has to climb with the row index -- it implements
        :class:`~django_data_shape.distributions.ascending.Ascending` and says
        so. A column filled by
        :class:`~django_data_shape.derivations.after.After` or by a skew has no
        order to be last in.

        And the fan-out has to be ``placement="grouped"``, which is the case
        this package spends most of its documentation warning about. Under
        ``grouped`` a parent's children occupy consecutive row indices, so a
        column climbing with the index climbs within every group and the last
        position really is the greatest value. Under ``arrival`` the children
        are interleaved *on purpose* -- that is what makes the physical layout
        honest -- so the group's rows are scattered through the index and the
        last position is an ordinary one of them.

        **That is a genuine incompatibility rather than a missing feature.**
        The two declarations say opposite things about the relationship between
        a group and the row order, and the refusal names both ways out. Nothing
        is lost from the plan by dropping it: PostgreSQL keeps no statistic
        about which row of a group holds which value, so the selectivity, the
        plan and the cost are identical either way. What ``order_by`` buys is
        that the active project is the newest one, which is realism for the
        application rather than for the planner.
        """
        column = declared.order_by
        ordering = self._fields.get(cast("str", column))
        if ordering is None:
            raise InvalidShape(
                f"{self.model.__name__}.{name} orders each group by {column!r}, which this shape "
                f"does not fill. Its columns are: {', '.join(sorted(self._fields))}."
            )
        if not isinstance(ordering, Ascending) or not ordering.is_ascending():
            raise InvalidShape(
                f"{self.model.__name__}.{name} orders each group by {column!r}, but {ordering!r} "
                "does not climb with the row index, so there is no last row of a group for it to "
                "mean. Fill that column with a Sequential going forwards, or drop order_by -- "
                "the invariant holds either way, because which row of a group is special is not "
                "something the planner can see."
            )
        fan_out = cast("FanOut", self._fields[declared.relation])
        if fan_out.placement != "grouped":
            raise InvalidShape(
                f"{self.model.__name__}.{name} orders each group by {column!r}, but "
                f"{declared.relation} has placement={fan_out.placement!r}, which interleaves a "
                "parent's children through the table on purpose. Their row indices are then "
                f"scattered, so the last row of a group is not the greatest {column}. Declare "
                "placement='grouped' to make the claim true and accept the clustered layout, or "
                "drop order_by -- it changes no plan, only which row of the group is the special "
                "one."
            )

    def _check_satisfiable(self, known: dict[str, Field[Any, Any]]) -> None:
        """Refuse a declaration the database provably cannot hold.

        A ``Constant`` on a unique column with more than one row is not a subtle
        problem: it is arithmetic, decidable here, and it used to be discovered
        by the database partway through a load that had already written most of
        a table. The same reasoning covers a ``Skew`` offering fewer values than
        there are rows.

        Only single-column uniqueness is checked. Multi-column constraints are
        satisfiable through combinations across independently declared columns,
        which is a real analysis rather than a comparison, and guessing at it
        would refuse declarations that are perfectly buildable.
        """
        for name, distribution in self.fields.items():
            field = known[name]
            if not field.unique or not isinstance(distribution, Bounded):
                continue
            available = distribution.distinct_values()
            if available < self.rows:
                raise InvalidShape(
                    f"{self.model.__name__}.{name} is unique and needs {self.rows} distinct "
                    f"values, but {distribution!r} can only produce {available}. The database "
                    "would refuse this partway through the load."
                )

    def _resolve_defaults(self, known: dict[str, Field[Any, Any]]) -> None:
        """Decide what happens to every field the caller did not declare.

        The subtlety that makes this its own method: **a Django ``default=`` is
        not a database default.** It is applied by ``save()``, and this package
        never calls ``save()`` -- it streams tuples into ``COPY``. So a column
        that is ``NOT NULL`` with a Python-level default has nothing behind it
        at the database level, and omitting it from the load fails on a
        not-null violation rather than quietly taking the default.

        Filling it in with the same value ``save()`` would have written keeps
        the caller's mental model true instead of making them restate a default
        they already declared on the model.
        """
        undeclared = [
            (name, field)
            for name, field in known.items()
            if name not in self.fields and not field.primary_key
        ]

        # Omitting a required relation used to be accepted and then fail inside
        # COPY with a not-null violation, so it is refused here instead.
        #
        # The message names the remedy rather than a limitation, and that is the
        # correction rather than the check: it used to say relations were not
        # supported and that fan-out was coming in the next release, which was
        # true when it was written and had been wrong for every release since.
        # Forgetting one foreign key is the commonest possible mistake, so this
        # is the first thing many readers ever see the package say -- and what
        # it said was that the package could not do the thing it is for.
        # `has_db_default` rather than `has_default`, and the difference is the
        # whole of the second fix here. A Python-level `default=` on a foreign
        # key is applied by save(), which this package never calls, so it puts
        # nothing behind the column -- and it used to exempt the column from
        # this refusal *and* from the fill below, which skips relations. Neither
        # half noticed it, and the load died inside COPY on the not-null
        # violation this refusal exists to replace. A real db_default is DDL, so
        # a column carrying one can be left out of the COPY and still be filled.
        #
        # Folding the default into a Constant, which is what happens to a scalar
        # a few lines down, would be the wrong repair: a key that did not come
        # out of the parent's table is a key drawn from nothing, which is
        # exactly what declaring a value distribution on a relation is refused
        # for.
        required_relations = sorted(
            name
            for name, field in undeclared
            if field.is_relation and not field.null and not has_db_default(field)
        )
        if required_relations:
            raise InvalidShape(
                f"{self.model.__name__}.{', '.join(required_relations)} cannot be null, so every "
                "row needs a parent and this shape does not say which. A relation is declared as "
                f"a fan-out over the parent's real keys -- {required_relations[0]}=FanOut(Zipf()) "
                "for the realistic heavy tail, FanOut(Uniform(1, 10)) for something flatter, and "
                "childless= for the share of parents nobody references. The parent's own table "
                "has to be built in the same shape or already loaded, because that is where the "
                "keys are read from."
            )
        undeclared = [(name, field) for name, field in undeclared if not field.is_relation]

        # A callable default is refused rather than guessed. This package cannot
        # know whether it varies per row -- ``uuid4`` does, ``dict`` does not --
        # and both readings produce data the application would never have
        # written: one duplicates a value meant to be unique, the other invents
        # variation where the model promised none.
        callables = sorted(
            name for name, field in undeclared if field.has_default() and callable(field.default)
        )
        if callables:
            raise InvalidShape(
                f"{self.model.__name__}.{', '.join(callables)} has a callable default, which "
                "this package will not call on your behalf: it cannot tell a per-row default "
                "from a shared one, and guessing either way writes rows the application never "
                "would. Declare a distribution for it."
            )

        missing: list[str] = []
        for name, field in undeclared:
            if field.has_default():
                self._fields[name] = Constant(field.get_default())
            elif field.null or has_db_default(field):
                continue
            else:
                missing.append(name)

        if missing:
            raise InvalidShape(
                f"{self.model.__name__}.{', '.join(sorted(missing))} cannot be null and has no "
                "default, so it has to be declared. A column left to chance is the column whose "
                "selectivity the plan then depends on."
            )

    def canonical(self) -> object:
        """Everything about this table that decides a row. See ``Canonical``.

        The model's label as well as its table name, because a project can
        rename either without touching the other and a digest that read only one
        would call two schemas the same.

        The fields are **sorted**, unlike a skew's weights: they become a
        ``COPY`` column list that ``columns()`` sorts anyway, so declaration
        order provably does not reach a single row, and sorting means two
        spellings of one declaration share a cached database instead of building
        it twice.
        """
        return (
            str(self.model._meta.label),
            self.db_table,
            self.rows,
            {name: self._fields[name] for name in sorted(self._fields)},
            self.keys,
            {name: self._statistics[name] for name in sorted(self._statistics)},
        )

    def __repr__(self) -> str:
        return f"Table({self.model.__name__}, rows={self.rows})"
