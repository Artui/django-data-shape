# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.18.0] — 2026-09-05

### Added

- **`Projection(..., max_rows=N)`: a declared ceiling, checked before the insert.**

  A projection is the one declaration with no `rows=`, deliberately -- its
  cardinality comes from the join, which is what reproduces a correlation a
  `FanOut` on the child would destroy. The consequence is that the largest table
  in a database can be the one nobody declared a size for, and it grows as a
  **product**: when both sides of the join fan out over the same parents, the
  busy parents multiply, so raising either declared count by four grows the
  result by sixteen. A consumer measured 2,413,223 rows against a declaration
  whose largest number was 300,000.

  The count is taken first and compared, so a declaration that has run away
  costs a scan of the join rather than the time to write every row of it. It is
  exact rather than estimated: the derived form counts the same join the insert
  selects from, and a `sql=` projection is counted by wrapping the caller's own
  select, because this package cannot know what that statement is one row per.

  The refusal names the number it would have written and the tables the join is
  over, because the surprise is never the ceiling -- a reader told only that a
  limit was exceeded still has to work out which of the two counts moved.

  **A declaration that does not ask is not charged for the answer**: with no
  ceiling, no count is taken. There is no default ceiling and there will not be
  one -- how many rows is too many is a judgement about size, which this package
  does not make on a caller's behalf anywhere else either.


## [0.17.1] — 2026-09-05

### Fixed

- **A scaled world left the identity sequence pointing at rows that came back**,
  a regression introduced by 0.17.0 and found by the consumer that prompted it.

  0.17.0 made `scaled_world` empty the tables its shape declares, so a session
  world could sit under one, and the rows come back because the emptying happens
  inside the transaction it rolls back. The **sequence** does not: `setval` is
  not transactional, so the counter kept whatever the scaled build moved it to.
  A scaled world is usually *smaller* than the session world it was built over,
  which left the counter below the ids that had just returned.

  The symptom was an `IntegrityError` on a primary key, in a later test, for a
  row the failing test never wrote.

  The sequences are now recomputed once the transaction has ended -- from
  `max(pk)` of whatever actually survived, rather than from a number captured on
  the way in, so it is correct in both directions and correct too when the
  caller's block raised partway through the build.

  The Postgres statement-count constant moved from 17 to 19: one reset per
  declared table, a property of the declaration rather than the factor, so what
  those tests pin is unchanged. The portable constant did not move, because
  Django emits no sequence reset for a SQLite table without `AUTOINCREMENT`.

## [0.17.0] — 2026-09-05

### Added

- **`Product`, `Offset` and `Copied`: the commonest arithmetic, said as data.**
  They compute nothing `Derived` could not. They exist because of what `Derived`
  costs: it takes a callable, a callable cannot be honestly digested, and a
  shape holding one is refused by `template_database`. So a column as ordinary
  as `total = quantity * unit_price` took a whole declaration out of the reuse
  that turns a forty-second build into a hundred-millisecond clone -- and
  `build()` kept working, so nothing said what it had cost.

  The refusal stays; it is right. These three are pure data, implement
  `Canonical`, and hash. `Derived` remains the answer for computation that
  really is code.

  `Offset` is also the same-row half of `After`, which is parent-scoped only.
  Its gap is fixed rather than spread across a window, because a due date thirty
  days after an issue date is a term and not a distribution.

### Changed

- **A scaled world can now be built over a session world.** The two pytest
  surfaces this package ships wanted the same tables in any application with one
  model graph, and were mutually exclusive: the session world holds its rows for
  the whole run, so the scaled build met a table that was not empty and refused.
  The documented answer -- give the two different models -- is not available to a
  project whose plan assertions and growth assertions are about the same flow,
  because that is what the application is.

  It was also order-dependent, which is what made it worth fixing rather than
  documenting better: a suite whose growth tests happened to be collected first
  passed, and the same tests named in the other order failed.

  `scaled_world` now empties the tables its shape declares before building.
  **Nothing is snapshotted, and nothing needs to be**: this happens inside the
  atomic block it already rolls back, so whatever was there comes back when the
  block ends. A table whose keys are `Disjoint` is left alone, mirroring the
  exemption `build` already makes, so the documented hybrid -- parents made by
  your code, children made here -- keeps working.

  `build()` keeps its refusal. It has no transaction of its own to undo and
  would be destroying rows for good.

  The two statement-count constants moved: one TRUNCATE on PostgreSQL, one
  DELETE per declared table elsewhere. Both are properties of the declaration
  rather than the factor, so what those tests pin -- fixed on PostgreSQL at
  every factor, a curve off it -- is unchanged.

### Fixed

- **`After` across a parent column of a different kind is refused.**
  `date + timedelta` is a `date`, so a `DateTimeField` child whose parent column
  was a `DateField` was filled with dates: `COPY` accepted them and they landed
  as naive midnights. A world whose orders went on sale in 2024 had shows
  starting in 1900, and the only signal was Django's own per-row
  `RuntimeWarning` in the middle of a build that prints thousands of lines.

  Both field types are on `_meta` and both are known before a row is generated,
  so this belonged in the class of things already refused at declaration time.
  Only `After` is checked, deliberately: `After` *is* `parent + offset` written
  into this column, so the two describe the same quantity by construction, while
  a `Derived` reading a parent column may legitimately convert -- turning a
  timestamp into a count of days is an ordinary thing to declare.

## [0.16.0] — 2026-09-04

### Fixed
- **A `FanOut(parents=[...])` partition no longer depends on what the keys are.**
  Keys are read back ordered by primary key and the sizes are assigned by
  position, so the weights followed the *sort order of the values* rather than
  the declaration. With integer keys that is merely surprising; with the UUID
  primary keys a factory row has on a modern schema it means **the same shape
  builds differently every run** -- measured, one declaration gave the
  first-named parent 5, 11 or 79 rows across twelve builds. That is the promise
  the package rests on, and the same reason `UuidKeys` derives rather than
  draws, so a narrowing that broke it was a defect and not a preference. Named
  parents are put back in the order they were named before anything is weighed.
  - **Reversing the list is now a different declaration**, which is what a
    reader writing one would expect and was the symptom reported.
  - **Weights are still scattered across positions**, so the reasoning that
    argued for scattering survives: a caller's *order* decides which key lands
    where, and nothing correlates a parent's key with its child count.
  - Worth knowing for anyone testing near this: a test written over
    **integer** parents passes against the broken behaviour, because the
    database's sort order matches insertion order there. Only UUID parents show
    it.
- **`shape_from_factory` takes `defaults=`, and names a factory that cannot be
  called.** Most factories in a real codebase take arguments and need them; a
  `TeamFactory` wanting a `permission_role`, called with nothing, left a
  required column empty and failed as a raw `IntegrityError` naming a
  constraint. That said nothing about what the caller ran, which is the one
  thing every other refusal here avoids. Anything the factory raises is now
  named with the call number, because "it failed" and "it failed after three"
  are different bugs.


## [0.15.0] — 2026-09-04

### Added
- **`shape_from_factory` runs a factory you already have and returns source, not
  a `Shape`.** That is the design rather than a limitation of it: a shape this
  package builds is *declared*, which is what makes it reviewable and
  assertable, and a shape learned from a sample is neither -- one used directly
  would change whenever the factory did, silently. So it returns text, and a
  person decides what of it to keep.
  - **What it finds matters more than what it writes**, because a faithful
    reading of a typical factory produces the flat world this package exists to
    argue against. Factories are written for single-object tests, so they fix
    values and reach foreign keys in the two most unrealistic ways there are.
    Every such column is a **finding**, and the report leads with them.
  - **A sub-factory is the sharpest case and the reason to run it.**
    `company = SubFactory(CompanyFactory)` creates one parent per child, which is
    a fan-out of degree one: every parent has exactly one row, the average is the
    truth, and a join over it cannot be misestimated. It is invisible in the
    factory's own source, so it is detected by watching which other tables grew
    and by how much. A round-robin over four parents is the same defect with a
    different number, and is reported too.
  - A relation is rendered as a `FanOut` and never as a value distribution,
    because `Table` refuses the second -- emitting it would hand back source that
    cannot be built, which is worse than a wrong number. A factory that already
    varies everything gets a declaration and **no findings**: if the quiet case
    were not quiet, the loud one would stop meaning anything.
  - Nothing is left behind. The calls run inside a transaction that is rolled
    back, so it can be pointed at a development database without writing to one.
    The sample size is stated in the output, because the answer moves with it.


### Fixed
- **`Md5Keys` could not fill a projection on about half of all tables.**
  `key_sql` wrote `to_hex(<stream>::bigint)`, asking PostgreSQL to re-derive a
  number Python produced as **unsigned** -- and `bigint` is signed, so any
  stream above 2^63 raised `NumericValueOutOfRange` before a row was written.
  The stream is a hash of the table and field names, so it is a coin flip per
  table rather than anything about a schema or a seed: measured, 49% of table
  names land above the limit. The stream is a constant by the time the statement
  is built, so the sixteen hex digits are embedded and nothing is re-derived.
  - **Reported by a consumer, and it had never executed for them.** In 0.13.0
    the join-ambiguity refusal answered first, so `key_sql` was unreachable;
    fixing that in 0.14.0 with `through=` is what exposed a feature 0.13.0 had
    shipped and nobody could run.
  - **The test that was supposed to prove the two halves agree used
    `stream=12345`** -- a number chosen for a test rather than one the producer
    makes. It now uses a pair `field_stream` actually produced, one either side
    of the limit, and asserts that the pair straddles it so a later change to
    how streams are derived cannot quietly make both tests vacuous.


## [0.14.0] — 2026-09-03

### Added

- **`Paired` fills the second key of a many-to-many, and the edge count stays
  exact.** Two fan-outs on one through table are refused because a fan-out is a
  partition computed from the row index alone, so two of them partition the same
  rows without either seeing the other -- nothing enumerates the pairs and a
  collision is a matter of the seed. `person=Paired("company", Zipf())` is the
  half that looks: `company` partitions the rows as any fan-out does, and within
  each of its groups `person` takes that many **distinct** partners. A duplicate
  pair is impossible by construction rather than removed afterwards, so nothing
  is deduplicated and the build never reports an achieved count against a
  requested one. The design notes for this milestone had given that up; they
  were wrong, and the rule that cardinality is declared survives M2M.
  - **What binds is the busiest group, not the product.** Every row of one
    group needs a different partner, so the constraint is
    `largest group <= partners` rather than `rows <= groups x partners` -- and a
    heavy tail puts a large share of every edge on one group, `Zipf(1.2)` over
    five thousand of them putting 21% on the top one. A declaration that looks
    sparse against the product can still be impossible, and it cannot be known
    until the partition is resolved: this is the one structural refusal that
    waits for the build rather than firing where the shape is written.
  - **The second side is derived and approximate, and the numbers are
    published.** Both marginals plus the edge count is over-determined, and
    fixing all three is a constraint satisfaction problem that cannot stream
    into `COPY`. Measured against exact weighted sampling over 200,000 edges,
    the busiest partner comes out 1.09 times as busy, the 99th percentile about
    30% high, and about 7% fewer partners are touched.
  - **Partners are allocated across weight bands and strided within one**, so
    nothing is ever drawn and asked whether it is taken. Rejection sampling
    degenerates exactly where the shape is most interesting -- 245 probes per
    row on that world -- and the usual fix, sampling the ones to leave out when
    a group wants more than half, is ten times faster and **a different sampling
    rule**, so a group one row over the halfway mark would get a materially
    different membership from one row under. The band count is **derived from
    the declared weights** rather than chosen, which systematic allocation is
    what makes possible: it converges as bands get finer where largest-remainder
    runs away, so "enough" is a limit rather than a number somebody tuned.

- **`FanOutPlan.group_of`** answers which parent a row belongs to, which a
  pairing needs to choose partners inside this row's group.

- **`Projection(..., through=Model)` says which model a derived join runs on.**
  An abstract base carrying `created_by`/`updated_by` to `User` -- a pattern most
  Django schemas have -- makes **any two models in the schema joinable**, so the
  derivation had candidates everywhere and could settle nothing: not for one
  pair but for every pair, which put the derived form out of reach for a whole
  application and left `sql=` as the only route to this feature's own motivating
  example. The refusal now names `through=` first, and names a model that can
  actually resolve the join rather than the first one alphabetically -- an audit
  model is reached by two edges from each side, so it is a real candidate and
  never a usable answer. Where every candidate is reached more than once,
  `through=` cannot narrow anything and the refusal says so instead of
  suggesting it.

### Changed

- **The two-fan-out refusal names the declaration that replaces it.** Its remedy
  used to be a statement of your own, because nothing else could keep the
  constraint. It now names `Paired` first and keeps `Projection(columns=, sql=)`
  as the escape hatch for an edge set that has to be a particular one.

### Fixed

- **A rounded `Uniform` can size a fan-out.**
  `FanOut(Uniform(1, 10, places=0))` was refused with "needs numeric fan-out
  sizes, but produced `Decimal('3')`" -- which reads as a contradiction, and was
  one keyword away from `FanOut(Uniform(1, 10))`, the spelling this package's own
  docstring recommends and two of its refusals suggest. `places=0` is the natural
  way to say that a fan-out size is a count. The guard was
  `isinstance(weight, (int, float))` while the next line already did
  `float(weight)`, so it was stricter than the arithmetic it protected. It is now
  the numeric tower plus `Decimal`, which `numbers.Real` deliberately excludes,
  and `Fraction` and numpy scalars come along with it. `bool` stays refused. The
  message also names a remedy now, which it was alone in not doing.

- **`columns=` accepts the column an `INSERT` actually lists.** The refusal that
  sends a reader there says "the select's columns are what the insert lists", and
  what an insert lists for a foreign key is `event_id` -- which was then refused
  as "no field named event_id". That is a contradiction rather than a correction,
  and the surrounding documentation reinforced the wrong reading. Both spellings
  are accepted and resolve to the same column, the messages say so, and the same
  dict is read for `statistics=` so the two cannot disagree about what a column
  is called.

## [0.13.0] — 2026-09-03

### Added
- **`FanOut(parents=[...])` narrows a fan-out to the parents it names**, which
  is the only gap two independent consumers reached separately: a tenant-scoped
  schema could not pin a foreign key to one pre-existing company, and a
  `GenericForeignKey` fanned out over every row of `django_content_type`. Real
  keys, narrowed by the database rather than filtered afterwards, and a named
  key that matches no row is refused **by key** rather than quietly taking a
  smaller share -- that failure is silent otherwise, because the rows that would
  have gone to it go to the others instead, or nowhere if it was the only one.
  A list of keys and never a queryset: a declaration needs no connection, and
  evaluating one here would give it one.
  - It reaches the arithmetic as well as the load. Capacity for a narrowed
    column is the **named** count, so the pigeonhole no longer overstates the
    room by the ratio of the two, and the same substitution makes the
    no-collision exemption read the named count. `fan_out_sizes` no longer
    reports `WorldChanged` for a narrowed partition, because a parent table
    holding more rows than the partition covers is the declaration working --
    and a named key that has since gone is refused earlier, by name.
  - **The documentation argues against the obvious use**, because the obvious
    use is against this package's own thesis: a column pinned to a single parent
    has `n_distinct = 1`, so a tenant filter is priced at nothing and looks free
    in a test database and does not in production. The recipe is several tenants
    with yours among the heavy ones, reached through `fan_out_sizes`.
- **`Md5Keys` is a UUID key strategy with a SQL twin**, so a projection can fill
  a UUID-keyed table. A projection has no declared row count, so its rows never
  pass through Python and its keys are written by the statement that inserts
  them -- and `blake2b` has no PostgreSQL equivalent, which is where every
  UUID-keyed projection used to stop. `md5` exists on both sides. **A separate
  strategy, never a second meaning for `UuidKeys`**: the two draw different keys
  for the same row, so substituting one for the other would change every key in
  every world already built. The two halves are compared against a real server
  rather than against a checked-in fixture, which would only prove the generator
  had not changed. The projection refusal now names it as the remedy.
- **`Disjoint`, the seventh opt-in protocol**, which is how a key strategy says
  its keys cannot collide with rows already in the table.

### Fixed
- **A UUID-keyed table can be built beside rows your own code made.** Building
  over a non-empty table is refused because this package assigns keys from 1 and
  a second build collides -- and that sentence is about integer keys, where the
  refusal was not. Both UUID strategies derive a 128-bit digest per row and
  cannot land on a factory's row, so the refusal blocked the hybrid this package
  documents in exactly the schemas where UUID primary keys are the norm.
  `SequentialKeys` still collides and is still refused, and `KeyFunction` is
  read as able to collide because this package cannot read your function.


## [0.12.0] — 2026-09-03

### Fixed

- **An OR of equalities over one column is read like the set it is.**
  `Q(status="DRAFT") | Q(status="IN_REVIEW") | Q(status="APPROVED")` says exactly
  what `Q(status__in=[...])` says, and an ORM gives no reason to prefer either --
  so reading one and not the other made the arithmetic depend on which of two
  equivalent spellings a caller happened to write. It was reported by the same
  consumer whose constraint the `__in` work was done for, who pointed out before
  any test existed that a fix aimed at that return statement specifically would
  miss this one. It did. The decoder now recurses, so a nested `Q(Q(a) | Q(b))`
  is read too, a value named twice counts once, and the line has not moved: an
  OR whose branches name **different columns** describes no set for any one
  column and is still declined, along with a negation, clauses joined by AND, a
  comparison in either spelling, and an `__in` over a queryset.

- **A required foreign key that carries a `default=` now says why the default
  does not fill it.** The message said the shape did not say which parent, and
  from the caller's side they had said: they wrote `default=` on the model, and
  every other tool they use honours it. It now names the default, says that
  `save()` is what applies one and that this package never calls `save()`, and
  adds what a callable default costs -- a callable default on a foreign key
  usually reads the database, which is the one thing a value for a row must
  never do.

- **"There is room" is never "there is a way", and the fan-out was never what
  was special.** Three more declarations that the arithmetic passed and the load
  then failed on, seed-dependently. All three are the one sentence the fan-out
  refusals already made: every mechanism this package has for filling a column
  computes it from the row index alone, so nothing enumerates a tuple and
  nothing keeps a column distinct. The exemption in every case is the `Distinct`
  protocol, unchanged.
  - **A single `unique=True` column filled by a draw** was refused only when the
    distribution offered fewer values than there were rows. Capacity is the
    wrong instrument: five rows over fifty values is ten times the room and
    loaded seventeen runs in twenty, ten rows over a hundred loaded ten. A shape
    that usually works is the case these refusals are for, because the run it
    fails is somebody else's. One row is exempt, having nothing to collide with.
  - **A composite uniqueness with no fan-out at all** was unchecked, because
    both existing refusals looked for a partition first. Fifty rows over three
    hundred combinations, six times the room, loaded **zero** runs in twenty --
    and over ten thousand combinations, **two hundred times** the room, still
    failed two.
  - **A `FanOut` on a `unique=True` column**, which fell between the two checks:
    `unique=True` writes a unique *index* and no entry in `_meta.constraints`,
    so the shape-level loop never saw it, while a single table steps over a
    fan-out because deciding it needs the parent's row count. A skewed partition
    exists to give some parent several children and a one-to-one permits none,
    so it never loads -- zero in twenty. The message names the shape that does:
    flat sizes over at least as many parents as rows.

- **A partial `UniqueConstraint` whose condition names a set is checked, not
  skipped.** `Q(status__in=["DRAFT", "IN_REVIEW", "APPROVED"])` was read as a
  predicate this package should not interpret, so the constraint was passed over
  in silence -- while the identical declaration spelled `Q(status="DRAFT")` was
  refused at declaration time with the arithmetic in it. The set form is the
  commoner one in real schemas, because "one open review per project" is
  open-ness across several statuses far more often than one, so the spelling
  that went unchecked was the likelier spelling. An equality is now read as a
  set of one and nothing downstream distinguishes them: what decides the
  refusal is whether a row can land inside the condition, and membership answers
  that either way. A set the caller wrote out is a list of values -- deciding
  membership in it needs no query planner, which is the line the original
  refusal was drawn on and the reason it drew it in the wrong place. A negation,
  two clauses joined, a comparison and an `__in` over a queryset are all still
  declined.

- **`PerParent`'s `rest` has to sit outside the condition, and now says so.**
  The set form makes reachable a case the equality form made hard to write:
  `last="DRAFT", rest="IN_REVIEW"` puts *every* row of every group inside a
  three-status condition, so the declaration that answers the equality form
  breaks this one. The remedy in the message names the requirement rather than
  leaving `rest=` an ellipsis.

- **A required foreign key carrying a Python-level `default=` is refused with
  the other undeclared required relations.** It fell between the two halves that
  should have caught it: the refusal skipped any column with a default, and the
  fill that would have supplied one skips any column that is a relation. So it
  was neither refused nor filled, and the load died inside `COPY` on the
  not-null violation the refusal exists to replace. Folding the default into a
  `Constant` would have been the wrong repair -- a key that did not come out of
  the parent's table is a key drawn from nothing, which is what declaring a
  value distribution on a relation is refused for. A real `db_default` is DDL
  and is still left to the database.

### Documentation

- **The one-to-one spelling has an example.** `FanOut(Constant(1))` over at
  least as many parents as the table has rows, and `Constant(1)` rather than
  `Uniform(1, 1)`, which is refused. The refusal for anything skewed is quoted
  with its measurement, because "a skew is probably wrong here" and "it loaded
  zero times in twenty" are different claims.

## [0.11.0] — 2026-09-03

### Added
- **`Distinct` is the sixth opt-in distribution protocol, and it says a column
  writes a different value in every row.** It is the exact dual of `Bounded`:
  that one says how few values a distribution can produce, this one says it
  never repeats. It exists because a pair is distinct as soon as either half is,
  so a column that answers `True` keeps a multi-column `UniqueConstraint` on its
  own with nothing arranged around it -- which is what separates the declaration
  that builds from the one that is a lottery. `Sequential` implements it for any
  non-zero step, in either direction: this is injectivity and not order, so it
  is a claim of its own rather than a second reading of `Ascending`.
- **`Projection(..., reads=(Model, ...))` says what a statement of your own
  selects from.** Nothing here parses SQL, so a raw `sql=` projection was a
  black box that could only be run last -- and last stopped being right the
  moment something fanned out over the table it fills, because the projection
  then has to run *before* that table and may find its own inputs still empty.
  `reads=` puts it back in the graph precisely: after what it reads, before what
  reads it. It is part of the declaration the template-database cache keys on,
  because the same statement run before and after a table selects different
  rows. Declaring it on a derived projection is refused -- `per=` and `copying=`
  already are that answer -- and so is naming the projected model itself, which
  would otherwise have surfaced as a cycle from inside the ordering pass.

### Changed
- **A load-order cycle is refused where the `Shape` is declared, not where the
  build starts.** Which table can be filled first is decided by the declarations
  and needs no connection to answer, so this was the one purely structural
  refusal left until build time: a shape that could never be built could be
  constructed, digested and passed around before anything said so.

### Fixed
- **A table fanning out over a raw `sql=` projection is no longer reported as a
  cycle that does not exist.** The refusal read
  `Assignment -> TimeEntry -> Assignment` for what is a chain, and there was no
  `after=` or `reads=` anywhere for the caller to correct it with. The cause was
  "a raw projection is ordered after every table" being expressed as an edge to
  every other declaration: an edge is a claim, it met the caller's own declared
  edge coming back, and the cycle detector was right about the graph it was
  given. Running such a statement last is a **preference**, so it is expressed
  as a visit order instead -- the graph now holds declared edges only, and
  everything that says what it reads is placed before anything that does not. A
  preference cannot contradict a declared edge, and two preferences that
  contradict each other are simply both dropped rather than reported as
  impossible.
- **Multi-table inheritance is refused by name instead of raising a bare
  `KeyError` from inside the loader.** `_meta.concrete_fields` for an inheriting
  child spans two tables while `db_table` names one, so declaring the parent's
  columns was accepted and then failed in the statistics pass with `KeyError:
  'title'` -- no message, no field, no mention of inheritance -- while omitting
  them hit a required-column refusal about a column the child could never have
  filled. There was therefore no working spelling at all. Both routes into a
  table, `Table` and `Projection`, now refuse the model up front and say what
  the obstruction is: one logical row is two physical rows sharing a key, this
  package fills one table per declaration and owns that table's keys, and
  declaring the two halves separately does not help because the child's primary
  key is a foreign key to the parent and a fan-out is a partition rather than a
  bijection. A proxy model is not this case and stays declarable.
- **A through table whose uniqueness spans two fan-outs is refused at
  declaration time instead of dying inside `COPY`.** The pigeonhole pre-check
  passed it happily, and correctly: the product of two parent counts dwarfs the
  row count, so there is ample room. Room was never the question. A fan-out is a
  partition of this table's rows over one parent's keys, computed from the row
  index alone, so two of them partition the same rows without either seeing the
  other -- which pairs come out together is an artefact of that index, and a
  collision is a matter of the seed. The refusal names the constraint, both
  fan-outs, and the form that does build a deduplicated edge table today: a
  `Projection` with `columns=` and your own `sql=`. One exemption, and it is a
  proof rather than a probability: a fan-out that provably gives no parent two
  rows never repeats a parent key, so no two rows share that column and the pair
  is distinct on that half alone. **One** of the two is enough -- twenty rows
  over twenty companies partitioned flat, beside a `Zipf` over five people,
  loads every time, and that person fan-out could not satisfy the proof at those
  numbers. The conditions are flat sizes, no `childless` share, the parent
  declared in the same shape, and `rows <= parents`; one row past that bound
  some parent gets two and the refusal comes back.
- **A fan-out beside a drawn column under one uniqueness is refused too, and
  for the same reason a second fan-out is.** `Table(Seat, rows=100,
  company=FanOut(Zipf()), label=Skew({"a": 1, "b": 1}))` over fifty companies
  has capacity exactly one hundred, passed every check, and died inside `COPY`
  at row 17 -- at a different row for the next seed. The proof does not need the
  second column to be a partition: a `Distribution` is by contract a pure
  function of the row index and of a draw derived from the field name and that
  same index, so it cannot see which parent the fan-out gave its row, nothing
  enumerates the pairs *inside a group*, and a group of three rows collides over
  two labels whatever the table's total capacity says. No arithmetic decides it
  either, because the quantity that would is the largest group the fan-out
  produces and that is not known until the partition is resolved at build time.
  The pigeonhole check still answers first where it applies, so a shape that
  does not fit at all keeps its arithmetic; a shape that fits and cannot be
  arranged now gets a refusal naming the fan-out, the drawn columns, and the
  form that does keep such a constraint -- `Derived(relation, compute=...,
  scope="group")`, which receives this row's position among its parent's
  children. Two exemptions, and both are proofs rather than probabilities: a
  column that is `Distinct`, because then there is nothing to arrange; and the
  same non-colliding partition the entry above describes, because a collision
  here is always two rows of one group and there is then no such group. Neither
  is "this usually works" -- that is the case these refusals are *for*, and the
  measured ones sit at ten and eleven times out of twenty.
- **A forgotten foreign key is told how to declare one, rather than told that
  relations are unsupported.** The message said "relations are not supported
  yet, so this shape cannot be built. Declaring fan-out as a distribution is the
  next release" -- true when it was written, wrong from the release after, and
  still there several releases later. Forgetting one required foreign key is the
  commonest possible mistake, so it is the first thing many readers ever see
  this package say, and what it said was that the package could not do the thing
  it exists for. It now names the column, the fan-out that fills it, and where
  the parent's keys have to come from.

## [0.10.0] — 2026-09-03

### Fixed
- **`scaled_shape` no longer drops the rules and targets the shape declared.**
  It rebuilt each `Table` without `statistics` and the `Shape` without
  `invariants`, so a business rule stopped being checked in every scaled world --
  which is every world a growth assertion builds -- and a table needing a raised
  statistics target could not be scaled at any factor, including 1. Neither
  raised, neither warned, and the suite passed either way: the build succeeded
  and the declaration simply meant less than it said. A test now asserts that
  scaling at factor 1 leaves the shape digest unchanged, so a field lost in the
  copy shows up without needing a test per field, and a second one fails the day
  a parameter is added to `Table` or `Shape` that this function does not forward.

## [0.9.0] — 2026-09-02

### Added
- **`check_constraints`, `apply_statistics_targets` and `require_clone_strategy`
  are exported.** All three were documented on the reference page and reachable
  only by their full module path. Because each shares a name with the module it
  lives in, `from django_data_shape import check_constraints` did not fail -- it
  bound the *module*, and the error arrived later at the call site as "module
  object is not callable". Documenting a symbol is a claim that it can be
  imported, so the claim is now true rather than the entries removed.
- **The inversion a skew exists for is reachable from outside.**
  `fan_out_sizes(shape, Order, "company")` returns a `ChildrenPerParent` -- an
  ordinary read-only mapping from parent key to child count, plus `ranked()` for
  the head and the tail and `childless()` for the parents nobody references. A
  fan-out was made a partition of the child key range rather than a draw per
  child *because* a partition can be inverted; until now the property was
  private, so a consumer wanting to assert on the busiest parent had to
  `GROUP BY` the child table first -- an aggregate over the entire world, run
  inside the session about to measure a plan, to recover something the
  declaration already knew. This costs one `SELECT` over the **parent** table
  instead, which is the asymmetry worth having at fifty thousand parents and two
  million children.
- **It is recomputed rather than remembered, and that is what makes it survive a
  cache hit.** A `template_database` build happens once and every later run
  clones, generating nothing at all, so anything carried off a `BuildResult`
  would simply not exist on that path. The partition is a pure function of the
  declaration, the seed and the parent's primary keys, so it is re-derived
  through the very code the build runs -- the clone holds the parents, the
  declaration holds the rest, and the template key already covers this package's
  own version, so a database built by a release that drew differently is never
  the one being read.
- **`WorldChanged`**, for the one failure recomputation makes possible. The
  partition takes exactly one thing from the database, so a parent table that has
  gained or lost rows since the build yields a partition of a world nobody built
  -- every number plausible and every one of them wrong. Where the parent is
  declared in the same shape, which every cacheable shape is, that is now checked
  and refused by name. Where the parents came from the ORM there is nothing to
  check against and nothing is claimed.

### Notes
- **The `ty` floor was raised to `0.0.32`, because the declared one was false.**
  `ty==0.0.1a10` cannot parse the `[tool.ty.environment]` table this repository
  has shipped since its first commit -- it fails with a TOML parse error. The
  `lowest declared versions` job passed only because the resolver it runs under
  rounds the pre-release up. A floor nothing can actually resolve to is not a
  floor.
- **Fan-out sizes are not rank-ordered on the parent key**, in either direction,
  and this is now written down where a reader meets it -- on `FanOut` itself and
  beside the inversion. It is deliberate rather than an oversight: ordering the
  sizes on the key would put a correlation between a parent's id and its child
  count into the child table, and a correlated foreign key is planner-visible, so
  it would be this package manufacturing exactly the flattering shape it exists
  to remove. Reach for either end through `ranked()` or `childless()`, never
  through `id=1`. A test pins it so it cannot be tidied away by accident.
- **A session-scoped `shape_fixture` is there for tests that never asked for it.**
  The rows are committed outside every test's transaction, so an ordinary
  per-test fixture over the same model does not start from an empty table -- and
  because the session fixture is only instantiated when something requests it,
  the resulting failure appears in a full suite run and not when the file is run
  alone, in files that never mention this package. Documented beside the existing
  `scale_fixture` and `transaction=True` caveats. It is not detected, and cannot
  be: this package cannot see the other test, and the only mechanism that could
  -- intercepting writes to a model some shape owns -- is a per-row hook, which is
  the one thing this library refuses to have at all.

### Fixed
- **`ShapeNotEmpty` appears in the reference.** It is raised by `build` against a
  table that already holds rows, and was the one public exception the page never
  listed.
- **The reference page and `__all__` are checked against each other.** The page is
  hand-maintained and drift in it is invisible in both directions: mkdocstrings
  resolves a full dotted path without consulting `__all__`, so an entry naming an
  unexported symbol still builds, and a missing entry breaks nothing at all. A
  test now fails on either, and on the specific case where the name at the package
  root is a module that shadows the symbol.

## [0.8.0] — 2026-09-02

### Added
- **`PerParent`: one row of every group is different, and the rest are not.**
  `status=PerParent("company", last="ACTIVE", rest="COMPLETE")` covers one active
  project, one default address, one current period, one primary contact and
  N winners per contest -- and the count of special rows is **derived from the
  fan-out rather than declared**. Fifty thousand companies and two million
  projects means exactly fifty thousand `ACTIVE` rows, 2.5%, and nobody chose
  that number. `Skew({"ACTIVE": 0.1, ...})` beside the same fan-out asks for two
  hundred thousand of them: the same rule this package states everywhere else --
  a distribution over a fixed count, never a multiplier -- except that here one
  distribution is derived from another rather than declared beside it.
- **Assignment order is not emission order, and the partition is what reconciles
  them.** Saying "the last project" needs a group; keeping physical placement
  honest needs a parent's children emitted interleaved. Because a `FanOut` is a
  partition of the child key range rather than a draw per child, a row's position
  within its group and the size of that group are O(1) arithmetic on the row
  index and the seed. Nothing is buffered, nothing is sorted, and the rows still
  stream one at a time into `COPY`.
- **`order_by` is a claim that is checked, not a sort that is performed**, and it
  is refused where it cannot be true: the column has to climb with the row index
  (the new `Ascending` protocol, which `Sequential` answers from its step's
  sign) and the fan-out has to be `placement="grouped"`. `order_by` and
  `placement="arrival"` are **mutually exclusive by meaning** -- arrival
  interleaves a group's rows on purpose, so the last row of a group is not the
  newest one. Dropping it costs nothing a planner can see: PostgreSQL keeps no
  statistic about which row of a group holds which value.
- **A static pre-check against `Model._meta.constraints`, run when the `Shape`
  is declared.** It refuses with the arithmetic -- "`one_active_project_per_company`
  permits at most 50000 rows with `status='ACTIVE'`, one per (company);
  `Project.status` is filled by `Skew(...)`, which asks for 200000 of them" --
  rather than leaving a unique index to fail at row 700,000 of a load that has
  already run for a minute. `_meta.total_unique_constraints` is the helper that
  sounds right and is the wrong one: it deliberately excludes conditional
  constraints, so it skips exactly this case.
- The pre-check lives on the **shape** rather than the table, because that is
  where both numbers are: a table knows its own row count and only a shape knows
  how many companies there are. It also does the multi-column pigeonhole a single
  `Table` declines -- two million rows needing distinct `(company, label)` pairs
  against fifty thousand companies and three labels -- and it says in its own
  docstring what it cannot decide: a condition that is not a single equality, a
  constraint over expressions, a group no fan-out partitions, a distribution that
  cannot enumerate itself, and a fan-out with a null share, because PostgreSQL
  counts each NULL in a unique index as distinct.
- **`Invariant` and `check_invariants`: rules checked as SQL after the load.**
  Either a `Q` describing the rows that are **wrong** -- run through
  `_base_manager`, so a filtering default manager cannot hide them -- or a whole
  statement whose every returned row is a violation. This is the only net that
  covers rules the database does not enforce, which is most of them.
- **A violated invariant fails the build**, raising `InvariantViolated` from
  inside the transaction that loaded the rows, so nothing lands and the database
  is left as it was found. The alternative is a database full of impossible data
  for every later assertion to be evaluated against, passing or failing for
  reasons unrelated to the code. The message names the rule and quotes the rows,
  because a build failure is read out of a terminal rather than stepped through.
- **An invariant changes no row and is still part of `shape_digest`.** The
  check runs during the build, and a cache hit is exactly what skips the build --
  so a rule that made no difference to the key would be a rule that silently
  stopped running the second time, which is worse than no rule because it is a
  rule everybody believes.
- `Categorical`, the fourth opt-in protocol: a distribution that names its values
  and their shares. It is what turns a partial `UniqueConstraint` from an error
  message at row 700,000 into arithmetic at declaration time, and what lets
  `PerParent` accept a `Skew` for `rest` only after proving it cannot also emit
  the special value. Deliberately not merged into `Bounded`: counting how many
  values a distribution has is a cheaper claim than enumerating them, and joining
  them would have made the cheap one cost the expensive one.
- `Ascending`, the fifth: whether a distribution's values rise with the row
  index. A bool rather than a marker, because `Sequential` with a negative step
  falls -- and a declaration asking for the newest row of each group while
  filling the column backwards would silently get the oldest.
- `Scope.GROUP`, so `PerParent` is a face of the existing derivation mechanism
  rather than a second one beside it. Its sources name a declared `FanOut` and
  resolve to a `(position, size)` pair.
- An `Invariants` documentation page, and the worked example in the README.

### Changed
- `Shape` takes `invariants=`, and validates the declared tables against their
  models' constraints before returning. A shape that could not be loaded now
  raises at the point it is written.


## [0.7.0] — 2026-09-02

- **Statistics targets, declared per column.** `Table(..., statistics={"status":
  500})` and the same on `Projection` issue `ALTER TABLE ... ALTER COLUMN ... SET
  STATISTICS` before the rows are loaded, so the `ANALYZE` that ends every build
  reads them. PostgreSQL keeps at most `statistics_target` most-common values and
  that many histogram bounds and samples 300 times as many rows, so this is the
  dial that decides how much of a declared shape the planner can record.
- **A target is declared, never inferred, and the distributions are read only to
  refuse.** A `Bounded` distribution offering more distinct values than its
  column's effective target is refused by name, with the number and the
  `statistics=` that would fix it. Choosing a target from the distribution would
  be this package deciding how the planner sees a column on evidence the
  declaration does not carry -- the same hundred-value skew wants a large target
  where those values are a predicate and wants nothing of the sort where they are
  not. So the luck is not replaced by a guess; it is made impossible to have
  without being told.
- Both orderings that decide whether any of it works are owned by the library: a
  target set after `ANALYZE` does nothing until the next one, and a refusal that
  costs a two-million-row `COPY` first is one nobody thanks you for. Both run
  before the load.
- **`shape_digest`: a content hash of a whole declaration**, stable across
  processes and equal for two shapes exactly when they would build the same
  rows. BLAKE2b over a tagged, length-prefixed encoding rather than Python's
  `hash()`, which is salted per interpreter run. Everything reachable
  contributes: row counts, distributions and their parameters, fan-outs with
  their childless and null shares and their placement, derivations, a
  projection's whole derived statement, key strategies, statistics targets and
  the seed.
- `Canonical`, the third opt-in protocol beside `Bounded` and `SqlKeys`: a
  declaration that is data says what it is made of, and a consumer's own
  distribution joins in by implementing it.
- **`UnhashableShape`, and what this refuses to hash.** `Derived` and
  `KeyFunction` each wrap a callable, and there is no honest digest of one: two
  lambdas share a name, a closure carries values from elsewhere, and identical
  bytecode returns something different when a constant it reads is edited in
  another module. Every one of those failures agrees while the data has changed,
  which is the one direction a cache key must never be wrong in. Such a shape is
  refused by name -- naming the table and the column -- rather than hashed
  approximately.
- **`template_database`: build a shape once per machine and keep it.** It creates
  the database under a working name, migrates, builds, and renames on success, so
  the existence of the final name is the same claim as "this one is finished".
  The name is a digest of the declaration, every migration on disk, every
  installed model's columns, `USE_TZ`, `TIME_ZONE` and this package's version, so
  a stale database is never asked for. A PostgreSQL advisory lock on the digest
  makes a parallel run build it once rather than once per worker, and connections
  to a finished template are turned off because the one failure mode of the whole
  mechanism is PostgreSQL refusing to copy a database something is attached to.
- **`clone_database`: `CREATE DATABASE ... TEMPLATE ... STRATEGY = file_copy`**,
  which is the operation that makes any of this worth doing. Measured here on a
  two-million-row, 183 MB database: the build is 16.6 s and the clone is 194-228
  ms, against 704-721 ms on PostgreSQL's default `wal_log`. The statistics and
  the per-column targets are ordinary catalogue contents and come with the copy,
  so a cloned database is planner-ready without gathering anything again.
- `drop_database`, because a content-addressed cache has nothing safe to garbage
  collect: nothing that survives is ever wrong, only unused, and dropping one on
  a guess would mean deleting a database because this package stopped recognising
  its name.
- `require_clone_strategy`, the second backend gate. `CREATE DATABASE ...
  STRATEGY` arrived in PostgreSQL 15, so an older server is refused by name with
  `strategy=None` as the way through -- and, like the vendor gate, it reads a
  version off the connection so the refusal is covered by passing one rather than
  by installing the server it refuses.
- A `Statistics and reuse` documentation page, with the `django_db_setup` recipe
  for cloning per session, the shorter version that goes through Django's own
  `TEST["TEMPLATE"]` setting, and what the cache does not support.
- Building a world on PostgreSQL now emits sixteen statements for a two-table
  shape rather than fourteen: statistics targets add one catalogue read per
  table. Still fixed whatever the scale factor, which is the property
  `scaled_world` documents and the suite pins.

## [0.6.0] — 2026-09-02

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

[Unreleased]: https://github.com/Artui/django-data-shape/compare/v0.18.0...HEAD
[0.18.0]: https://github.com/Artui/django-data-shape/compare/v0.17.1...v0.18.0
[0.17.1]: https://github.com/Artui/django-data-shape/compare/v0.17.0...v0.17.1
[0.17.0]: https://github.com/Artui/django-data-shape/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/Artui/django-data-shape/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/Artui/django-data-shape/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/Artui/django-data-shape/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/Artui/django-data-shape/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/Artui/django-data-shape/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/Artui/django-data-shape/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/Artui/django-data-shape/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/Artui/django-data-shape/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/Artui/django-data-shape/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/Artui/django-data-shape/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/Artui/django-data-shape/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Artui/django-data-shape/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Artui/django-data-shape/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Artui/django-data-shape/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Artui/django-data-shape/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/Artui/django-data-shape/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Artui/django-data-shape/compare/v0.0.0...v0.1.0
