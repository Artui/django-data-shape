# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `FanOut`, which declares how a foreign key's children spread across their
  parents: a size distribution, a `childless` share for parents with no children
  at all, a `null` share for nullable columns, and `placement`.
- `Zipf`, the heavy-tailed weight distribution fan-out is realistically drawn
  from. A table where every parent has ten children is not merely tidy -- it is
  the one shape in which the planner is never wrong, because its `n_distinct`
  average is the truth.
- Tables load in dependency order, and a cycle of fan-outs is refused by name.

### The two representation decisions
- **Fan-out reads the parent's real keys rather than assuming the dense `1..N`
  range this package assigns.** The case that matters is the hybrid: a project
  builds its fifty companies with the ORM, where the row count is small and the
  ORM is the right tool, and asks this package only for the two million orders.
  Referential integrity then holds by construction, because every key emitted
  came out of the parent table.
- **A fan-out is a partition of the child key range, not a per-child draw.**
  Parent `j` owns rows `[start, end)`. A per-child draw cannot be inverted, and
  "which children belong to parent T" is what a mirrored collection needs. The
  childless tail and `placement` both fall out of the partition for free.

### Notes
- `placement` defaults to `arrival`. Emitting children parent by parent gives a
  perfectly clustered table that no production system has and that flatters
  every index scan over the foreign key.
- A self-referential fan-out is refused: it would read keys from a table still
  empty at load time. Self-referential trees are their own feature.
- A relation needs a `FanOut` and a plain column refuses one, in both
  directions.

## [0.1.1] — 2026-09-01

### Added
- `Bounded`, an optional second protocol for distributions that can say how many
  distinct values they produce. `Constant` and `Skew` implement it. It is
  separate from `Distribution` on purpose: adding the method there would make it
  required, so a custom distribution written against the single-method protocol
  would stop satisfying it.
- A declaration that provably cannot be loaded is now refused at declaration
  time. A `Constant` on a unique column with more than one row, or a `Skew` with
  fewer values than rows, is arithmetic rather than a subtle problem, and it used
  to be discovered by the database partway through a load that had already
  written most of a table. Only single-column uniqueness is checked; multi-column
  constraints are satisfiable through combinations across independently declared
  columns, which is an analysis rather than a comparison.

### Fixed
- `Uniform` with `places` raised `decimal.InvalidOperation` past 28 significant
  digits -- Python's default context precision -- from inside the `COPY` loop, on
  a column such as `numeric(30, 2)` that would have accepted the value. The
  precision needed is now derived from the declared bounds.
- `Table` and `Shape` attributes are read-only. Every rule they enforce runs once
  in `__init__`, so while the attributes were writable a declaration could be
  edited afterwards into one that would have been refused, with nothing
  re-checking it.

## [0.1.0] — 2026-09-01

### Added
- The shape vocabulary: `Shape`, `Table`, and the `Skew`, `Uniform`, `Sequential`
  and `Constant` distributions, behind a single-method `Distribution` protocol.
- `build()`, which generates rows, loads them with `COPY FROM STDIN`, moves the
  identity sequence past the keys it assigned, and runs `ANALYZE`. The order is
  owned by the library: loading into a table analyzed while empty leaves the
  planner applying old statistics to a new row count, which is a worse lie than
  having no statistics at all.
- `InvalidShape`, raised at declaration time, and `UnsupportedBackend`, raised
  for any connection that is not PostgreSQL. Generation is backend-neutral;
  `COPY` and planner statistics are not, and degrading quietly would produce the
  false confidence this package exists to remove.
- Primary keys are assigned as a dense `1..N` range rather than declared. That is
  what will let a foreign key be satisfied without a lookup once relations land,
  and what makes a self-referential tree acyclic by construction.
- A field carrying a Django `default=` is filled with the value `save()` would
  have written, because defaults are applied by `save()` and nothing here calls
  it. A callable default is refused instead of guessed: `uuid4` varies per row
  and `dict` does not, and nothing on the field distinguishes them.

- `ShapeNotEmpty`, raised before anything is written when a target table already
  holds rows. Keys start at 1 on every build, so the collision was previously a
  unique-violation naming an index, which says nothing about what to do instead.
- The whole build runs in one transaction, so a shape whose second table fails
  leaves nothing behind and can be re-run after a fix.
- Every declared value passes through its field's `get_db_prep_save`. Without it
  a naive datetime was stored verbatim rather than localised -- hours from where
  `save()` puts it under a non-UTC `TIME_ZONE` -- and a `JSONField` could not be
  written at all.

### Notes
- Relations are refused in both directions: declaring one raises, and so does
  omitting one that cannot be null. An optional foreign key may be omitted and
  loads entirely `NULL`, which is documented rather than left to be discovered.
- Only integer primary keys are supported. Any other kind is refused, rather than
  a dense `1..N` integer range being written into a character column.
- psycopg 3 is required and psycopg 2 is refused by name. Rows stream straight
  into `COPY FROM STDIN`, which psycopg 2 cannot do without materialising them
  first.

[Unreleased]: https://github.com/Artui/django-data-shape/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Artui/django-data-shape/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Artui/django-data-shape/compare/v0.0.0...v0.1.0
