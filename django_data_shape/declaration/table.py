from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from django.db.models import Field, Model

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.declaration.order_derivations import order_derivations
from django_data_shape.derivations.after import After
from django_data_shape.derivations.derivation import Derivation
from django_data_shape.derivations.per_parent import PerParent
from django_data_shape.derivations.scope import Scope
from django_data_shape.distributions.ascending import Ascending
from django_data_shape.distributions.bounded import Bounded
from django_data_shape.distributions.constant import Constant
from django_data_shape.distributions.distinct import Distinct
from django_data_shape.distributions.distribution import Distribution
from django_data_shape.keys.infer_key_strategy import infer_key_strategy
from django_data_shape.keys.key_strategy import KeyStrategy
from django_data_shape.relations.fan_out import FanOut
from django_data_shape.relations.gives_no_row_two_children import gives_no_row_two_children
from django_data_shape.relations.paired import Paired
from django_data_shape.relations.tree import Tree
from django_data_shape.utils import (
    check_not_inherited,
    check_statistics_target,
    has_db_default,
    many_to_many_refusal,
    offsettable_kind,
    primary_key_field,
)

# What both refusals of a required key to this same table offer: neither a Tree
# (its first row has no parent to point at) nor a FanOut (its keys are read from
# a table still empty) fills one, and the wording is 0.25.1's, from shape_from_factory.
_SELF_KEY_REMEDY = "Make the column null=True, or fill the table with a Projection."


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
    :func:`~django_data_shape.loading.apply_statistics_targets.apply_statistics_targets`
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
        fields: Mapping[str, Distribution | FanOut | Tree | Derivation] | None = None,
        keys: KeyStrategy | None = None,
        statistics: Mapping[str, int] | None = None,
        **field_distributions: Distribution | FanOut | Tree | Derivation,
    ) -> None:
        if rows < 0:
            raise InvalidShape(f"{model.__name__} cannot have {rows} rows.")

        declared: dict[str, Distribution | FanOut | Tree | Derivation] = dict(fields or {})
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

    # Read-only: every rule is enforced once in __init__, and a hashable shape must not change.
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
    def fields(self) -> Mapping[str, Distribution | FanOut | Tree | Derivation]:
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
        # Inheritance first: every check below reads concrete fields that may span two tables.
        check_not_inherited(self.model)
        meta = self.model._meta
        known = {field.name: field for field in meta.concrete_fields}

        unknown = sorted(name for name in self.fields if name not in known)
        if unknown:
            if (refusal := many_to_many_refusal(self.model, unknown)) is not None:
                raise InvalidShape(refusal)
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

        mismatched = sorted(
            name
            for name, declared in self.fields.items()
            if known[name].is_relation and not isinstance(declared, (FanOut, Paired, Tree))
        )
        if mismatched:
            raise InvalidShape(
                f"{self.model.__name__}.{', '.join(mismatched)} is a relation, so it needs a "
                "FanOut rather than a value distribution or a derivation. Either would emit keys "
                "drawn from nothing, pointing at rows that may not exist. The second key of an "
                "edge table is a Paired instead, which chooses partners distinct inside the "
                "fan-out beside it, and a key to the table's own rows is a Tree."
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
                "and a fan-out reads keys from a table that is still empty at load time. A key "
                f"to the table's own rows is a tree: {self_referential[0]}=Tree(roots=0.05, "
                "sizes=Zipf()) gives each row a parent at an earlier row, or none for the share "
                "declared as roots."
            )
        misapplied = sorted(
            name
            for name, declared in self.fields.items()
            if not known[name].is_relation and isinstance(declared, (FanOut, Paired, Tree))
        )
        if misapplied:
            raise InvalidShape(
                f"{self.model.__name__}.{', '.join(misapplied)} is not a relation, so a FanOut "
                "has nothing to fan out over."
            )
        self._check_trees(known)
        self._check_pairings(known)

        self._resolve_defaults(known)
        self._check_statistics(known)
        self._check_satisfiable(known)
        self._check_derivations(known)
        self._computation_order = order_derivations(self.model.__name__, self._fields)

    def _check_trees(self, known: dict[str, Field[Any, Any]]) -> None:
        """A ``Tree`` fills a nullable key to this same table, and nothing else."""
        for name, declared in sorted(self.fields.items()):
            if not isinstance(declared, Tree):
                continue
            where = f"{self.model.__name__}.{name}"
            field = known[name]
            related = cast("type[Model]", field.related_model)
            if related is not self.model:
                raise InvalidShape(
                    f"{where} points at {related.__name__}, not at {self.model.__name__}, and a "
                    "Tree's parents are earlier rows of this same table. A key to another table "
                    f"is a fan-out over that table's real keys: {name}=FanOut(Zipf())."
                )
            if not field.null:
                raise InvalidShape(
                    f"{where} cannot be null, and a tree's roots are the rows whose parent is "
                    "NULL. The first row is always one, since there is no earlier row for it to "
                    "point at, so no tree fits a column that refuses NULL. " + _SELF_KEY_REMEDY
                )
            if field.unique and not gives_no_row_two_children(declared, self.rows):
                raise InvalidShape(
                    f"{where} is unique, so each row may be the parent of one row at most, and "
                    f"{declared!r} spreads children over parents by weight, which gives some "
                    "parents several. A tree whose sizes hold one value gives no row two children "
                    f"and fills it: {name}=Tree(roots={declared.roots!r}, sizes=Constant(1)). Or "
                    "leave the column out and fill it after the build."
                )

    def _check_statistics(self, known: dict[str, Field[Any, Any]]) -> None:
        """Refuse a statistics target on a column this shape does not fill."""
        for name in sorted(self._statistics):
            where = f"{self.model.__name__}.{name}"
            if name not in known:
                if (refusal := many_to_many_refusal(self.model, [name])) is not None:
                    raise InvalidShape(refusal)
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
        """Refuse a derivation whose sources are not there to be read."""
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
            if isinstance(fan_out, Tree):
                raise InvalidShape(
                    f"{self.model.__name__}.{name} reads {source!r} across {relation}, which is "
                    "a Tree: the parent is a row of this same table, generated in the same pass, "
                    "and a root has no parent at all. Nothing reads across a tree; fill this "
                    "column from a distribution of its own."
                )
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
            if isinstance(declared, After):
                self._check_offset_units(name, source, parent, field_name, known[name])

    def _check_offset_units(
        self,
        name: str,
        source: str,
        parent: type[Model],
        field_name: str,
        column: Field[Any, Any],
    ) -> None:
        """Refuse an ``After`` whose parent column is a different kind of thing."""
        parent_field = cast("Field[Any, Any]", parent._meta.get_field(field_name))
        parent_kind = offsettable_kind(parent_field)
        column_kind = offsettable_kind(column)
        if parent_kind is None or column_kind is None or parent_kind == column_kind:
            return
        raise InvalidShape(
            f"{self.model.__name__}.{name} is After({source!r}), which writes that parent "
            f"column plus an offset into this one -- but {parent.__name__}.{field_name} is a "
            f"{type(parent_field).__name__} and {self.model.__name__}.{name} is a "
            f"{type(column).__name__}. Adding an offset to a "
            f"{type(parent_field).__name__} produces a {parent_kind}, so this column would be "
            f"filled with the wrong kind of value and the load would accept it. Read a "
            f"{type(column).__name__} on the parent, or fill this column with a distribution "
            "of its own."
        )

    def _check_group_sources(self, name: str, declared: Derivation) -> None:
        """Refuse a group-scoped column whose groups are not there to be grouped by."""
        for source in declared.sources:
            fan_out = self._fields.get(source)
            if isinstance(fan_out, Tree):
                raise InvalidShape(
                    f"{self.model.__name__}.{name} is decided per group of {source!r}, which is "
                    "a Tree. A tree's families are not a partition of the rows into ranges -- "
                    "each child is placed among the rows after its parent -- so there is no "
                    "position in a group to decide anything by."
                )
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
        """Refuse an ``order_by`` this package cannot make true."""
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

    def _check_pairings(self, known: dict[str, Field[Any, Any]]) -> None:
        """Every ``Paired`` has to name a fan-out in this same table."""
        for name, declared in sorted(self.fields.items()):
            if not isinstance(declared, Paired):
                continue
            where = f"{self.model.__name__}.{name}"
            partner = self.fields.get(declared.relation)
            if declared.relation == name:
                raise InvalidShape(
                    f"{where} is paired with itself. Paired names the *other* key of the edge -- "
                    "the fan-out whose groups this column takes distinct partners inside."
                )
            if partner is None:
                fanned = sorted(
                    other for other, one in self.fields.items() if isinstance(one, FanOut)
                )
                available = (
                    f"Its fan-outs are: {', '.join(fanned)}."
                    if fanned
                    else "It declares no fan-out at all, and a pairing takes its groups from one."
                )
                raise InvalidShape(
                    f"{where} is paired with {declared.relation!r}, which this table does not "
                    f"declare. {available}"
                )
            if not isinstance(partner, FanOut):
                raise InvalidShape(
                    f"{where} is paired with {declared.relation!r}, which is declared as "
                    f"{partner!r} rather than a fan-out. A pairing takes its groups from a "
                    "partition, and only a fan-out partitions this table's rows over a parent."
                )

    def _check_satisfiable(self, known: dict[str, Field[Any, Any]]) -> None:
        """Refuse a declaration the database provably cannot hold."""
        for name, distribution in self.fields.items():
            field = known[name]
            if not field.unique:
                continue
            if isinstance(distribution, Bounded):
                available = distribution.distinct_values()
                if available < self.rows:
                    raise InvalidShape(
                        f"{self.model.__name__}.{name} is unique and needs {self.rows} distinct "
                        f"values, but {distribution!r} can only produce {available}. The database "
                        "would refuse this partway through the load."
                    )
            if self.rows < 2:
                continue
            if isinstance(distribution, (FanOut, Paired, Derivation, Tree)):
                continue
            if isinstance(distribution, Distinct) and distribution.is_distinct_per_row():
                continue
            raise InvalidShape(
                f"{self.model.__name__}.{name} is unique and {distribution!r} draws a value per "
                "row rather than producing a different one every time, so whether two rows take "
                "the same value is a matter of the seed. Room is not what decides it: ten rows "
                "over a hundred values is ten times the room and loads in ten runs out of twenty. "
                "A column that is distinct in every row keeps the constraint on its own and says "
                "so with Distinct, which Sequential implements for any non-zero step. A column "
                "whose values have to be a particular set is filled by a Projection with "
                "columns= and sql= instead."
            )

    def _resolve_defaults(self, known: dict[str, Field[Any, Any]]) -> None:
        """Fill each undeclared field: a Django ``default=`` is not a database default."""
        undeclared = [
            (name, field)
            for name, field in known.items()
            if name not in self.fields and not field.primary_key
        ]

        required_relations = sorted(
            name
            for name, field in undeclared
            if field.is_relation and not field.null and not has_db_default(field)
        )
        if required_relations:
            defaulted = sorted(name for name in required_relations if known[name].has_default())
            because = (
                f" {', '.join(defaulted)} carries a default=, and a default is applied by save(), "
                "which this package never calls -- so it puts nothing in the column here. A "
                "callable one is not called on your behalf either, and a callable default on a "
                "foreign key usually reads the database, which is the one thing a value for a row "
                "must never do."
                if defaulted
                else ""
            )
            own_table = [
                name for name in required_relations if known[name].related_model is self.model
            ]
            elsewhere = [name for name in required_relations if name not in own_table]
            remedy = (
                f" A relation is declared as a fan-out over the parent's real keys -- "
                f"{elsewhere[0]}=FanOut(Zipf()) "
                "for the realistic heavy tail, FanOut(Uniform(1, 10)) for something flatter, and "
                "FanOut(Zipf(), max_children=1) for a one-to-one. childless= is the share of "
                "parents nobody references. The parent's "
                "own table has to be built in the same shape or already loaded, because that is "
                "where the keys are read from."
                if elsewhere
                else ""
            )
            if own_table:
                remedy += (
                    f" {', '.join(own_table)} points at {self.model.__name__} itself, and a "
                    "FanOut reads its keys from a table still empty at load time, so it is "
                    f"refused there. {_SELF_KEY_REMEDY}"
                )
            raise InvalidShape(
                f"{self.model.__name__}.{', '.join(required_relations)} cannot be null, so every "
                f"row needs a parent and this shape does not say which.{because}{remedy}"
            )
        undeclared = [(name, field) for name, field in undeclared if not field.is_relation]

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
