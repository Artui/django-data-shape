# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`Projection`: a table populated by `INSERT ... SELECT` over tables already
  built.** It covers the shape a distribution cannot -- a collection copied along
  a join, where the child count is *determined* rather than drawn. An `Event` is
  created from a `Template` and its `EventSession` rows mirror that template's
  `TemplateSession` rows, so how many sessions an event has is
  `count(TemplateSession WHERE template = event.template)` and nothing chose it.
- The statement is **derived from the model graph** for the common
  copy-the-collection case: `Projection(EventSession, per=Event,
  copying=TemplateSession)` is the whole declaration. The join is the model both
  sides reach in one step, the foreign key to `per` gets that row's key, a
  foreign key to `copying` gets the copied row's key, a column named the same as
  one on the source is copied from it, a plain `default=` is written as a bound
  parameter and a nullable column is left out. Zero and several candidate joins
  are both refused by name rather than guessed between.
- `sql=` with `columns=` is the escape hatch for anything shaped oddly. The
  columns are field names checked against the model, and the primary key has to
  be among them, because this package owns the keys and a statement it did not
  write has to say what they are.
- `SqlKeys`, an extension of `KeyStrategy` for a strategy that can also say
  itself in SQL. `SequentialKeys` implements it -- `row + 1` over a row index the
  database computes -- and it is what fills a projected table's key column.

### Why this rather than a vocabulary for mirroring
- **It is what the real system already collapses into at scale.** One event built
  from a template is a service call; a million is one statement, and a projection
  is that statement. It is also the honest answer to the per-row creation hook
  this package declines: the need underneath that request is real, and this meets
  the collection half of it without making the default path anything but a set
  operation.
- **It needs no new distribution machinery at all** -- no mirror mode, no
  inverted fan-out, no derived-cardinality vocabulary.
- **It reproduces a correlation PostgreSQL cannot see.** Sessions-per-event is
  correlated with the template, so events from a big template all have many
  sessions. A `FanOut(Zipf())` on `EventSession.event` draws that count
  independently and produces a join selectivity real data never has.

### Decisions worth carrying
- **There is no `rows=` on a projection, and that is the point.** Its cardinality
  is decided by the tables it copies from, so declaring it too would be the
  over-determination this package refuses everywhere else. The achieved count
  comes back in the `BuildResult`, which is what that type was built to report --
  and it means `scaled_shape` passes a projection through untouched, because
  scaling the tables it reads scales it by exactly the same amount.
- **A projected table's keys come from the same place as every other table's.**
  The strategy on the declaration decides them; it just has to have a SQL form,
  because there is no declared row count to enumerate in Python and the rows
  never pass through it. `UuidKeys` and `KeyFunction` are refused by name rather
  than approximated with a different hash in SQL, which would give one strategy
  two meanings depending on which statement filled the table.
- **A projection sits in `build()`'s one loop, not in a pass of its own.** Only
  the step that produces the rows differs; the emptiness check, the sequence
  reset and the `ANALYZE` after it are the same steps for the same reasons.
  **The `ANALYZE` above all**: rows arriving by `INSERT ... SELECT` are as
  invisible to the planner as rows arriving by `COPY`, and statistics describe a
  table rather than the query that filled it.
- **A projected table may itself be a fan-out parent.** Running every projection
  last would have forbidden that by scheduling accident rather than by anything
  true about the data, so `order_tables` now sorts the whole declaration graph --
  fan-out edges and projection edges alike -- and a cycle is refused by name. A
  raw `sql=` projection names nothing it reads, because nothing here parses SQL,
  and is ordered after every table and every derived projection instead.
- **A projection that inserts no rows fails the build.** An empty projected table
  is not a smaller world; it is a declared table left out of the database, and
  every test reading it then passes or fails for a reason unrelated to the code.

### Fixed
- **`ScaleProtocol` yields `int | None`, not `int`.** Its docstring invited a
  consumer on an unsupported backend to supply a five-line callable, and the
  obvious five-line callable builds rows and just yields -- which `ty` rejected.
  **This was the second time the same protocol's prose promised more than its
  signature allowed**, the first being the parameter name that 0.4.0 fixed by
  making `factor` positional-only, and both were found by a consumer rather than
  by review. A caller reading the value now has to tolerate `None`; an
  implementation that can count cheaply should still yield the number.
  `tests/scale_protocol_consumers.py` carries the invited implementation itself,
  so the type-level promise has a type-level test behind it -- which is what was
  missing both times.
- `ScaleProtocol`'s docstring and the pytest page now give the exact
  `Callable[[int], AbstractContextManager[int | None]]` spelling, so a consumer
  restating the shape rather than importing it converges on one form instead of
  a looser `ContextManager[Any]`.
- **The "open a query capture inside the block" hazard is repeated on
  `scale_fixture`.** It lived only on `scaled_world`, which the world *author*
  calls; the person who can make the mistake is writing the test and reaches the
  fixture without ever opening the other function.
- **The statement-cost measurement is pinned by tests instead of stated in
  prose.** It said twelve statements for a two-table shape and the real number is
  fourteen. Two tests now hold both halves: the PostgreSQL cost is the same at
  every factor, and the portable cost grows by one statement per thousand rows --
  which is the half that matters, because it is a curve with the same shape as
  the one a growth assertion is trying to measure.

### Changed
- `Shape` accepts a `Table` or a `Projection`. Declaring one model as both is
  refused by the same check that refuses declaring it twice -- it is the same
  over-determination, and worse for being harder to see.

### Internal
- `primary_key_field` and `has_db_default` moved to `utils`, where both routes
  into a table can ask them. A composite-primary-key refusal worded two ways is
  a refusal that will drift.

## [0.5.0] — 2026-09-02

### Added
- **The derivation mechanism**: one scope-parameterised thing rather than four
  bespoke ones. `Derived`, `After`, `Given` and `Aligned` all ask the same
  question -- compute this from something already known -- and differ only in
  **where the inputs are read from**: this row, the parent row, or a shared rank.
  `Derived` is the mechanism and takes `scope=` directly, so a correlation
  nobody shipped a face for is still declarable; the other three are shorthand
  over it and contain no resolution logic at all. Built separately, "custom
  creation logic" and "correlate across a relation" become two vocabularies
  overlapping on the half a consumer asks for first.
- `Derived("quantity", "unit_price", compute=operator.mul)` computes a column
  from other columns of the same row. `compute` receives the resolved sources
  positionally and nothing else -- not the row index, not a draw, because a
  function of either of those is already a distribution and would be a
  planner-visible declaration the planner-facing half could not see.
- `After("account.signed_up_at", within=timedelta(days=365))` puts a child's
  column a spread gap past its parent's. It works in whatever unit the column
  uses, and the documentation says plainly that the result is not monotonic with
  the row, so it has a low `pg_stats.correlation` where `Sequential` has a high
  one.
- `Given("account.plan", {...}, default=...)` chooses a distribution by the
  parent's value. An unlisted value with no default is refused during the load,
  naming the column and the value -- one of very few refusals here that cannot
  happen at declaration time, because the parent's values live in the parent
  table rather than in the declaration.
- `Aligned("size", Uniform(...))` reads a distribution at a rank shared with
  every column naming that rank, with `reverse=True` for a column related the
  other way. Independent marginals give a database that is realistic per column
  and unrealistic per entity: no single row is extreme in two ways at once, and
  that row is the one that breaks production. The coupling is exact and has no
  strength parameter, because a partial coupling is a copula.
- **A derivation reaches its parent through the fan-out that already exists.** A
  parent-scoped source is read out of the parent table, in the same query that
  reads the parent's keys, so it costs one query per relation per build rather
  than one per row -- which a partition can do and a per-child draw could not.
  The values are **queried, not recomputed from a declaration**, so a parent
  built with the ORM behaves exactly like one built here; that is the same
  correction the keys took, applied to the columns beside them.
- **Column order is not computation order.** `Table.columns()` stays sorted by
  name because it is the `COPY` column list; derivations get a topological order
  of their own, and a cycle among them is refused at declaration time by name.
  `Table.computation_order()` reports it.
- **Generation runs under a query guard, and it is a real check.** This package
  may call your code, and your code may not call the database: a query issued
  while rows are being generated raises `DerivationQueriedDatabase`, naming the
  table, its derivations and the statement. That is what keeps a derivation from
  becoming the per-row creation hook this package exists to replace -- a hook
  whose body may query is a hook whose body will, and a package whose default
  path is not `COPY` has no reason to exist. The guard sees the connection being
  built; the rule holds beyond that and only its enforcement stops there.

### Changed
- A parent's columns are read through the ORM rather than a raw cursor when a
  derivation asks for them, because a raw cursor bypasses the field's own
  `from_db_value` and a key is the one column where that never shows. Measured:
  SQLite hands a raw `DateTimeField` back naive where the ORM hands back an
  aware datetime, so `After` would compute its offset from a value six hours
  from the one the application reads; a `JSONField` comes back as text rather
  than a dict. Keys alone still take the hand-written statement, which is what
  keeps every branch of the partition coverable without a connection.
- A relation declared with something other than a `FanOut` now says "a value
  distribution or a derivation" rather than naming only the first.

## [0.4.0] — 2026-09-02

### Added
- **The pytest surface**, in `django_data_shape.fixtures`. `shape_fixture(shape)`
  returns a session-scoped fixture that builds a shape once for a whole run;
  bind it to a name in `conftest.py` and request that name from a test. It
  composes with pytest-django rather than replacing it, asking for
  `django_db_setup` and `django_db_blocker` by name so the coupling is to two
  fixture names and not to pytest-django's internals. Session scope is
  load-bearing: pytest creates higher-scoped fixtures first, so the rows are
  committed before the transaction that wraps a test is opened, and every test
  sees them while everything a test writes is rolled back with it.
- **The scale protocol**, which is what a growth assertion asks a world for:
  make the world be at factor F, then let the caller run its block.
  `scaled_world(shape, factor)` is a context manager that builds it and undoes
  it; `scale_fixture(shape)` is the same thing as a fixture; and `ScaleProtocol`
  is the structural type both satisfy, so a consumer asserting that a query
  count is `O(1)` rather than `O(N)` depends on the shape of the call rather
  than on this package. What the context manager yields is a plain row count,
  not a `BuildResult`, for the same reason: a seam a stranger cannot implement
  is not a seam.
- `scaled_shape(shape, factor)`, the declaration transform underneath it. **A
  factor varies the declaration rather than subsetting one larger build**, because
  a subset is not a smaller database but the same database with a filter -- the
  statistics still describe every row, and the block under test would have to
  cooperate by restricting itself, which puts the harness inside the thing being
  measured. Every table scales, parents included, so the average fan-out is the
  same at every factor and two worlds differ in size alone. The scaled tables go
  through `Table`'s own constructor, so a declaration that only holds at its
  original size is refused at the factor that breaks it, naming the factor.
- `skip_unless_postgres(connection, operation)`, the pytest twin of the backend
  refusal. Both fixtures skip with the refusal's own message as the reason where
  a shaped database cannot exist, so a suite that also runs on SQLite reports
  what it did not check rather than passing over a database nobody shaped.
- A `pytest` extra. It is an extra rather than a dependency because the rest of
  the package has nothing to do with pytest, which is also why these fixtures are
  not re-exported from the top-level `__init__`: importing them is what requires
  pytest, not importing the package.
- **`build(shape, require_statistics=False)` loads rows on any backend.** It asks
  for rows and cardinality rather than for a database the planner can reason
  about, and it is written as a requirement being dropped rather than as work
  being skipped: on PostgreSQL it changes nothing at all, since `COPY` and
  `ANALYZE` are both free and leaving them out would manufacture the unanalyzed
  table this package exists to condemn. Elsewhere the rows are inserted in chunks
  and no statistics are gathered. SQLite's own `ANALYZE` is deliberately not run,
  because running it would claim the plan realism this package says it will not
  claim. The driver check is unaffected: psycopg 2 is still refused on a
  PostgreSQL connection, because the vendor picks the route and not the caller.
- **The growth harness works on every backend Django supports** and no longer
  skips. A query count is an ORM property and means the same anywhere, so a
  growth assertion is honest off PostgreSQL where a plan assertion is not, and
  the scale harness was the only thing standing between a consumer on SQLite and
  the milestone's headline seam. `shape_fixture` still skips: it exists to build
  a world a planner will believe.

### Fixed
- `ScaleProtocol` rejected the implementations its own docstring offered. A
  structural type matches parameter names too, so a hand-rolled callable taking
  `n` rather than `factor` did not satisfy it -- exactly the five-line callable
  the documentation tells a consumer to write. The factor is positional-only now,
  and two files of consumers and impostors are type-checked by the suite, because
  a type-level claim with no type-level test is what let this ship.
- `ShapeNotEmpty` names the likely cause and not only the remedy. The first
  consumer met it by composing both fixtures over one model, where the rows are
  real, correct and written by a fixture the failing test never mentions -- so
  "empty the table first" read as advice about somebody else's data.

## [0.3.0] — 2026-09-01

### Fixed
- **A UUID primary key no longer refuses to load.** It was refused outright,
  which made this package unusable for a whole class of Django project. The
  reframing is the fix: the design never needed integers, it needed a
  deterministic injection from row index to key -- which is what lets a foreign
  key be satisfied without a lookup, what makes a self-referential tree acyclic
  on the index rather than the value, and what makes two builds of one shape
  agree. Integers were only the most obvious such function.
- The primary key is prepared by its own field, like every other value. It did
  not need to be while keys were always integers, and a UUID works either way
  because psycopg adapts it -- but a key type needing conversion was stored
  verbatim, which is the bug already found once on an ordinary column.
- Two documentation examples were syntax errors -- `...` after keyword arguments,
  in the README and on the relations page -- and would have failed the moment a
  reader pasted them.
- The install line is quoted. `pip install django-data-shape[postgres]` fails in
  zsh with `no matches found`, which is the default shell on macOS.
- The README's usage example imports the model it uses, and documents the
  refusals a first attempt actually meets: PostgreSQL and psycopg 3, integer
  primary keys, empty tables, and callable model defaults.

### Added
- **Key strategies.** A table's primary keys come from a deterministic function
  of the row index rather than from a hard-coded dense `1..N` range. Integer keys
  count from one, `UUIDField` keys are derived from the seed, and `KeyFunction`
  declares one for any other type.
- `SequentialKeys`, `UuidKeys`, `KeyFunction` and the `KeyStrategy` protocol,
  plus a `keys=` argument on `Table`.
- A composite primary key is refused by name. It is not among a model's concrete
  fields, because it has no column of its own, so the package used to raise a
  bare `StopIteration` from inside itself. The message says `keys=` cannot help
  either: a strategy maps a row index to one value, and this is arity rather than
  type.
- The documentation's Python examples are parsed by the test suite. A docs
  example is the first code anybody runs, so it gets a guard rather than a
  convention.

## [0.2.0] — 2026-09-01

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

[Unreleased]: https://github.com/Artui/django-data-shape/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/Artui/django-data-shape/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Artui/django-data-shape/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Artui/django-data-shape/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Artui/django-data-shape/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/Artui/django-data-shape/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Artui/django-data-shape/compare/v0.0.0...v0.1.0
