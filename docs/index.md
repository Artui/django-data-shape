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

- **Primary keys are assigned here**, as a dense `1..N` range. That is what will
  let a foreign key be satisfied without a lookup when relations land, and what
  makes a self-referential tree acyclic by construction. The identity sequence is
  moved past them afterwards, so the first `objects.create()` in your test does
  not collide with a key that already exists. Only integer primary keys are
  supported, and a model with any other kind is refused rather than filled with
  numbers in a character column.
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
declaration time, naming the field. A generated database that is wrong is worse
than one that refuses to exist, because the suite it feeds asserts on rows that
could never occur.

`build()` raises `UnsupportedBackend` on anything but PostgreSQL. Generation and
cardinality are backend-neutral; `COPY` and column statistics are not, and a
shaped database whose plans mean nothing is worse than no shaped database.

## Not in this release

Foreign-key fan-out as a distribution, physical placement of related rows,
per-group invariants, many-to-many edges, and reusing a built database as a
template.

Relations are refused in both directions: declaring one raises, and so does
omitting one that cannot be null. An **optional** foreign key may be left out,
and loads entirely `NULL` -- which is worth knowing rather than assuming, because
a join key with `null_frac` of 1.0 is not a neutral thing to hand a planner.
