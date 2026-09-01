"""One model's declared shape."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, cast

from django.db.models import Field, IntegerField, Model
from django.db.models.fields import NOT_PROVIDED

from django_data_shape.distributions.bounded import Bounded
from django_data_shape.distributions.constant import Constant
from django_data_shape.distributions.distribution import Distribution
from django_data_shape.invalid_shape import InvalidShape


class Table:
    """How many rows of one model, and how each column is distributed.

    Field distributions are given as keyword arguments because that is the form
    a reader scans fastest. ``fields=`` is the escape hatch, and it is not
    optional politeness: a model may legitimately have a column called ``rows``
    or ``model``, and Python's own argument binding would silently hand that
    keyword to this signature instead. Without the mapping form those models
    would simply be undeclarable.

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
        fields: dict[str, Distribution] | None = None,
        **field_distributions: Distribution,
    ) -> None:
        if rows < 0:
            raise InvalidShape(f"{model.__name__} cannot have {rows} rows.")

        declared: dict[str, Distribution] = dict(fields or {})
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
    def fields(self) -> Mapping[str, Distribution]:
        """The declared distributions, including defaults filled in from the model."""
        return MappingProxyType(self._fields)

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

    def _validate(self) -> None:
        meta = self.model._meta
        known = {field.name: field for field in meta.concrete_fields}

        unknown = sorted(name for name in self.fields if name not in known)
        if unknown:
            raise InvalidShape(
                f"{self.model.__name__} has no field named {', '.join(unknown)}. "
                f"Its concrete fields are: {', '.join(sorted(known))}."
            )

        pk_fields = [field for field in known.values() if field.primary_key]
        for field in pk_fields:
            # The dense 1..N range this package assigns is integers, and nothing
            # downstream converts it. Given a CharField primary key the load
            # used to succeed and write "1", "2", "3" -- values the application
            # could never produce, with a whole statistics picture built on top
            # of them. Refusing is the only honest answer until a key strategy
            # can be declared per table.
            if not isinstance(field, IntegerField):
                raise InvalidShape(
                    f"{self.model.__name__}.{field.name} is a "
                    f"{type(field).__name__} primary key, and this package assigns primary keys "
                    "itself as a dense 1..N integer range. Only integer primary keys are "
                    "supported."
                )

        pk_names = {name for name, field in known.items() if field.primary_key}
        declared_pk = sorted(pk_names & set(self.fields))
        if declared_pk:
            raise InvalidShape(
                f"{self.model.__name__}.{', '.join(declared_pk)} is the primary key, which this "
                "package assigns itself as a dense 1..N range. That is what lets a foreign key "
                "be satisfied without a lookup, so it is not available to declare."
            )

        # Relations are the next release's work, and generating a foreign key
        # column from a value distribution would produce ids pointing at rows
        # that may not exist. Refusing is the only honest answer until fan-out
        # can be declared as a distribution over the parents.
        relations = sorted(
            name for name, field in known.items() if field.is_relation and name in self.fields
        )
        if relations:
            raise InvalidShape(
                f"{self.model.__name__}.{', '.join(relations)} is a relation, and relations are "
                "not supported yet. Declaring fan-out as a distribution is the next release."
            )

        self._resolve_defaults(known)
        self._check_satisfiable(known)

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

        # Declaring a relation is refused in _validate; omitting a required one
        # used to be accepted and then fail inside COPY with a not-null
        # violation. Both directions have to refuse, or the contract only holds
        # for the callers who tried the unsupported thing explicitly.
        required_relations = sorted(
            name
            for name, field in undeclared
            if field.is_relation and not field.null and not field.has_default()
        )
        if required_relations:
            raise InvalidShape(
                f"{self.model.__name__}.{', '.join(required_relations)} is a relation that "
                "cannot be null, and relations are not supported yet, so this shape cannot be "
                "built. Declaring fan-out as a distribution is the next release."
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
            elif field.null or self._has_db_default(field):
                continue
            else:
                missing.append(name)

        if missing:
            raise InvalidShape(
                f"{self.model.__name__}.{', '.join(sorted(missing))} cannot be null and has no "
                "default, so it has to be declared. A column left to chance is the column whose "
                "selectivity the plan then depends on."
            )

    @staticmethod
    def _has_db_default(field: Field[Any, Any]) -> bool:
        """Whether the database itself will supply a value.

        ``db_default`` arrived in Django 5.0 and this package supports 4.2, so
        the attribute cannot be assumed to exist. Unlike ``default``, this one
        is real DDL, which is why a column carrying it can be left out of the
        ``COPY`` entirely.
        """
        return getattr(field, "db_default", NOT_PROVIDED) is not NOT_PROVIDED

    def __repr__(self) -> str:
        return f"Table({self.model.__name__}, rows={self.rows})"
