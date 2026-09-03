# django-data-shape

A realistically shaped test database from Django models.

Declare what your data looks like -- how many rows, how skewed a column is, how
values advance -- then load it with `COPY` and `ANALYZE` it, so the query planner
makes the choices it will make in production rather than the choices it makes
over ten rows.

## Why not a loop

The obvious alternative is `for i in range(100_000): Order.objects.create(...)`,
and it is not just slower. It is wrong in the flattering direction:

- **Rows without statistics plan worse than no rows.** A freshly loaded table
  with no `ANALYZE` gives the planner a default selectivity guess, which is how a
  two-million-row table gets bitmap-scanned through an index for a value
  matching 98% of it.
- **Analyze-then-load is worse still.** Statistics gathered while the table was
  small get applied to the new row count. In the measurements this package was
  designed from, that produced a thirty-thousand-fold misestimate.

So the order -- generate, load, reset the sequence, analyze -- is owned by the
library rather than left to the caller to remember.

## A shape

```python
import datetime

from django_data_shape import Sequential, Shape, Skew, Table, Uniform, build

from myapp.models import Order

shape = Shape(
    Table(
        Order,
        rows=1_000_000,
        # The 98/2 split is what decides whether an index on status is usable.
        # Ten rows with one of each says the opposite of what production says.
        status=Skew({"complete": 0.98, "pending": 0.015, "cancelled": 0.005}),
        total=Uniform(0, 500, places=2),
        # Monotonic on purpose: Postgres costs an index scan differently
        # depending on how well a column correlates with physical order, and
        # shuffled timestamps are unrealistic in a way that only shows up in
        # plan choice.
        created_at=Sequential(
            datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
            datetime.timedelta(seconds=3),
        ),
    ),
    seed=1234,
)

result = build(shape)
print(result.rows)
```

## What it decides for you

- **Primary keys are assigned here**, by a *key strategy*: a deterministic
  function of the row index. Integer keys count from one and the identity
  sequence is moved past them afterwards; UUID keys are derived from the seed, so
  two builds of one shape agree. Determinism is the requirement, not integers --
  it is what lets a foreign key be satisfied without a lookup and what makes a
  self-referential tree acyclic. A key type with no obvious strategy is refused
  rather than guessed, and `keys=KeyFunction(...)` declares one. See
  [Keys](keys.md).
- **Every value goes through its field's `get_db_prep_save`.** Without it a naive
  datetime lands in the database verbatim rather than localised, which under a
  non-UTC `TIME_ZONE` is hours from where `save()` would have put it, and a
  `JSONField` cannot be written at all.
- **A Django `default=` is filled in, not skipped.** Defaults are applied by
  `save()`, and nothing here calls `save()`, so a `NOT NULL` column with a
  Python-level default has nothing behind it in the database. The value `save()`
  would have written is used instead.
- **A callable default is refused.** `uuid4` varies per row and `dict` does not,
  and nothing on the field distinguishes them. Declare a distribution instead.
- **A declaration is read-only once built.** Every rule runs in the constructor,
  so an editable declaration would be one that could be rewritten past its own
  validation.

## What it expects

- **The tables must be empty.** Keys start at 1 on every build, so building over
  existing rows would collide; `build()` checks first and raises `ShapeNotEmpty`
  before writing anything, rather than letting the database report a duplicate
  key.
- **The whole build is one transaction.** A shape whose second table fails leaves
  nothing behind, so re-running after a fix meets the same state the first
  attempt did.
- **psycopg 3.** Rows stream straight into `COPY FROM STDIN`, which psycopg 2
  cannot do without materialising them first. A psycopg 2 connection is refused
  by name.

## What it refuses

A declaration that cannot describe a database raises `InvalidShape` at
declaration time, naming the field. That includes ones which are merely
arithmetic: a `Constant` on a unique column with more than one row, or a `Skew`
offering fewer values than there are rows, is refused before a row is generated
rather than found by the database halfway through a load. A generated database that is wrong is worse
than one that refuses to exist, because the suite it feeds asserts on rows that
could never occur.

Two of them are refusals of a whole model shape rather than of a field.
**Multi-table inheritance** puts one logical row in two tables sharing a key,
and this package fills one table per declaration and owns that table's keys, so
it can write either half and has nothing to pair them with. A **through table
whose uniqueness spans two fan-outs** is refused for a different reason: it fits
comfortably, and nothing enumerates the combinations, so a collision is a matter
of the seed. Both used to be accepted and then fail during the load, one with a
bare `KeyError` and one with a unique violation inside `COPY`.

`build()` raises `UnsupportedBackend` on anything but PostgreSQL, because a
shaped database whose plans mean nothing is worse than no shaped database.

`build(shape, require_statistics=False)` is the other half of the same sentence.
Generation and cardinality really are backend-neutral, so it loads rows anywhere
-- with `COPY` and `ANALYZE` where the backend has them, plain inserts where it
does not -- and claims nothing about a plan. It is what the growth harness uses,
because a query count is an ORM property and means the same on any backend. On
PostgreSQL it changes nothing: the statistics are free, so they are still
gathered.

## Relations

Foreign keys are declared with a `FanOut` -- how many children each parent has,
as a distribution rather than a number, plus the childless tail and where the
children physically sit. The parents can be ones this package built or ones your
own code did. See [Relations](relations.md).

## Projections

Some tables are not distributed at all, they are copied. An event created from a
template has exactly the sessions that template has, so its child count is
*determined* rather than drawn -- and correlated with the template, which is the
cross-table correlation Postgres cannot see. `Projection` fills such a table with
one `INSERT ... SELECT` derived from the model graph, which is what a creation
service collapses into at scale. See [Projections](projections.md).

## Invariants

A company has many projects, at most one of which may be `ACTIVE`. That is a
partial `UniqueConstraint`, and with 50,000 companies and 2,000,000 projects it
means exactly 50,000 active rows -- a share *derived from* the fan-out rather
than declared beside it. `PerParent` generates it, declared `invariants` check it
as SQL after the load, and a static pre-check off `Model._meta.constraints`
refuses the contradiction with the arithmetic before a row exists. See
[Invariants](invariants.md).

## From pytest

A session-scoped fixture builds a shape once for a whole run, and a scale
harness makes the same world at several sizes so a query count can be asserted
to be `O(1)` rather than `O(N)`. The harness works on any backend, because a
query count is an ORM property; the session world skips with a stated reason
where a shaped database cannot exist, because its job is to be believed by a
planner. See [From pytest](pytest.md).

## Not in this release

Many-to-many edges as a declared form, multi-table inheritance, and emitting a
declaration from a database or a factory that already exists. The first two are
**refused by name** rather than left to fail during the load, and the refusal
for a through table points at the escape hatch that does build one today: a
`Projection` with your own `sql=`, which can select the pairs already
deduplicated.
