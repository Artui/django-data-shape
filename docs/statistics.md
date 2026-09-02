# Statistics and reuse

Two halves of one claim: the planner should see the shape you declared, and you
should not pay for it more than once per machine.

`ANALYZE` has run at the end of every build since 0.1.0, because a loaded table
with no statistics is the state this package exists to condemn. What it gathers
is bounded by each column's **statistics target**, and what it produces is worth
keeping, which is what the **template-database cache** is for.

Both are PostgreSQL-only, and they say so rather than degrading quietly.

## Statistics targets

PostgreSQL keeps, per column, at most `statistics_target` most-common values and
`statistics_target` histogram bounds, and samples 300 times that many rows to
find them. `default_statistics_target` is 100 out of the box. Everything past the
target is collapsed into a single residual frequency.

So a column with more distinct values than its target has a shape the planner
**cannot** record, however carefully it was declared:

```python
from django_data_shape import Shape, Skew, Table

from myapp.models import Event

# 150 event types, of which the planner would record 100.
Shape(Table(Event, rows=2_000_000, kind=Skew(weights)))
```

Ask for it, and the column carries it:

```python
from django_data_shape import Shape, Skew, Table

from myapp.models import Event

Shape(
    Table(
        Event,
        rows=2_000_000,
        kind=Skew(weights),
        statistics={"kind": 300},
    )
)
```

`statistics=` maps a field name to a number of buckets, on
[`Table`][django_data_shape.table.Table] and on
[`Projection`][django_data_shape.projection.Projection] alike. A column left out
keeps whatever target the schema gives it.

### Declared, never inferred

The target could have been derived from the distribution -- a `Skew` knows how
many values it has. It deliberately is not, and the reason is worth stating
because the alternative looks helpful.

A target is a property of the **column**, not of the distribution. The same
hundred-value skew wants a large target where those values are a query's
predicate and wants nothing of the sort where they are not, and no distribution
says which. Choosing on your behalf would mean this package deciding, silently,
how the planner sees a column, on evidence the declaration does not contain --
and then two builds of one declaration would differ because a default moved.

What the distributions *are* read for is a refusal. If a
[`Bounded`][django_data_shape.distributions.bounded.Bounded] distribution can
produce more distinct values than its column's effective target, the build stops
and names the column:

```text
Event.kind declares 150 distinct values and that column's statistics target is
100, so the planner would record 100 of them and estimate the rest from a single
residual frequency. The declared shape would be built and then not seen, which is
the state this package exists to expose rather than to produce. Ask for it --
statistics={'kind': 150} on this table -- or declare fewer values.
```

That is the answer to "a shape that gets a hundred buckets by luck is not the
same as one that asked". The luck is not replaced by a guess; it is made
impossible to have without being told.

A distribution that cannot count its values -- one drawing from a continuous
range, or a caller's own -- simply does not implement `Bounded` and is treated as
unbounded rather than as suspicious.

### Two orderings, both owned by the library

- The `ALTER TABLE ... SET STATISTICS` runs **before** the rows and therefore
  before the `ANALYZE`. A target changed afterwards does nothing at all until the
  next `ANALYZE`, which is the same trap as analyzing before loading.
- The refusal runs before the rows too, because a refusal that costs a
  two-million-row `COPY` first is a refusal nobody thanks you for.

It is a build-time refusal rather than a declaration-time one, unusually for this
package, and it has to be: the number it compares against lives in the server.

## The template-database cache

Building a shaped database is expensive and copying one is not. Measured here, on
a two-million-row table:

| Step | Cost |
| --- | --- |
| Build: generate, `COPY`, reset the sequence, `ANALYZE` | 16.6 s |
| Database size | 183 MB |
| `CREATE DATABASE ... TEMPLATE ... STRATEGY = file_copy` | **194-228 ms** |
| The same clone on PostgreSQL's default `wal_log` | 704-721 ms |
| `ANALYZE` on the cloned two-million-row table | 111 ms |

The statistics come with the clone -- `pg_statistic` rows and the per-column
targets in `pg_attribute` are ordinary catalogue contents -- so a cloned database
is planner-ready without gathering anything again. That is the whole ratio: about
seventeen seconds once per machine, and a fifth of a second per test database.

```python
from django_data_shape import clone_database, drop_database, template_database

template = template_database(shape)  # builds the first time, finds it after
clone_database(template, "test_myapp", replace=True)
```

[`template_database`][django_data_shape.template_database.template_database]
names the database after a content hash of everything that decides what is in it,
so reuse is safe rather than merely fast. Change the declaration, the schema, the
time-zone settings or this package's version and the name changes, so the old
database is simply never asked for again.

### The key

[`shape_digest`][django_data_shape.shape_digest.shape_digest] is the declaration
half, and it is public because it is useful on its own -- it needs no database and
answers "are these two shapes the same shape".

It is a BLAKE2b digest of a canonical encoding, not Python's `hash()`: that one is
salted per interpreter run, so a key built on it would be a different key in every
process. Everything reachable from the shape contributes -- row counts,
distributions and their parameters, fan-outs with their childless and null shares
and their placement, derivations, projections, key strategies, statistics targets
and the seed.

Two orderings are kept rather than sorted, because they reach the data: a `Skew`
lays its cumulative bounds out in the order its weights were written, and a shape
keeps its table order because a raw `Projection` falls back on it. A table's
*fields* are sorted, because they become a sorted `COPY` column list before a row
is generated.

### What it refuses to hash

[`Derived`][django_data_shape.derivations.derived.Derived] and
[`KeyFunction`][django_data_shape.keys.key_function.KeyFunction] each wrap a
callable you supplied, and there is no honest digest of a callable. Two lambdas
share a name; a closure carries values from elsewhere; and a function hashed down
to its bytecode still returns something different when a constant it reads is
edited in another module. Every one of those failures is in the same direction --
the key agrees while the data has changed -- and the result is a suite running
against a database built from code that no longer exists.

So a shape holding one raises
[`UnhashableShape`][django_data_shape.unhashable_shape.UnhashableShape], naming
the table and the column. Build it with `build()` and pay the load, or implement
[`Canonical`][django_data_shape.canonical.Canonical] on a declaration that really
is data:

```python
class EveryNth:
    """A distribution that is a function of its parameters, and says so."""

    def __init__(self, step: int) -> None:
        self._step = step

    def value(self, row: int, draw: float) -> object:
        return row * self._step

    def canonical(self) -> object:
        return (self._step,)
```

### From pytest

`template_database` and `clone_database` are the two halves; `django_db_setup` is
where pytest-django documents putting them. This replaces pytest-django's own
test-database creation:

```python
# conftest.py
import pytest
from django.db import connections

from django_data_shape import Shape, Skew, Table, clone_database, drop_database
from django_data_shape import template_database

from myapp.models import Order

SHAPE = Shape(Table(Order, rows=2_000_000, status=Skew({"complete": 98, "pending": 2})))


@pytest.fixture(scope="session")
def django_db_setup(django_test_environment, django_db_modify_db_settings, django_db_blocker):
    connection = connections["default"]
    settings = connection.settings_dict
    # pytest-django has already appended its xdist worker suffix to TEST["NAME"]
    # by the time this runs, so the name below is this worker's own database.
    target = settings["TEST"]["NAME"] or f"test_{settings['NAME']}"

    with django_db_blocker.unblock():
        clone_database(template_database(SHAPE), target, replace=True)
        connection.close()
        settings["NAME"] = target
        yield
        connection.close()
        drop_database(target)
```

There is a shorter version that stays entirely inside pytest-django's own
lifecycle, using Django's documented `TEST["TEMPLATE"]` setting. Django then
creates the test database itself, with its own `WITH TEMPLATE` clause and
therefore the server's default strategy -- about three and a half times slower
per session, and it re-runs `migrate` over the clone:

```python
# conftest.py
import pytest
from django.db import connections

from django_data_shape import template_database


@pytest.fixture(scope="session")
def django_db_modify_db_settings(django_db_modify_db_settings_parallel_suffix, django_db_blocker):
    with django_db_blocker.unblock():
        connections["default"].settings_dict["TEST"]["TEMPLATE"] = template_database(SHAPE)
```

`--reuse-db` is not worth reaching for with either: a clone is cheaper than
deciding whether the database you have is the one you want, and the template
*is* the reuse.

### What it does not support

- **Anything but PostgreSQL.** `CREATE DATABASE ... TEMPLATE` has no equivalent
  elsewhere, which is also the answer to whether the unit of reuse is a table set
  or a database. It is a database.
- **A shape whose declaration cannot be hashed**, as above.
- **Being called inside a transaction.** Filling a template means pointing the
  connection at another database and closing it, which leaves a connection
  unusable for the rest of an atomic block. It belongs in session setup and
  raises `TransactionManagementError` anywhere else rather than poisoning the
  connection.
- **A template on a different server from the database that clones it.**
  `CREATE DATABASE ... TEMPLATE` copies files within one cluster.
- **Cleaning up after itself.** A template is a content-addressed cache: nothing
  that survives is ever *wrong*, only unused, and dropping one on a guess would
  mean deleting a database because this package stopped recognising its name.
  They are all named `data_shape_` followed by a digest, and
  [`drop_database`][django_data_shape.drop_database.drop_database] removes one.
- **A `RunSQL` edited inside a migration that already exists.** The key covers
  every migration's name and every model's fields, so ordinary schema changes
  move it; editing the body of a migration that has already been created changes
  neither. Drop the template by hand when that happens.

Parallel runs *are* supported. Under `pytest-xdist` every worker asks for the
same template at once; the first takes a PostgreSQL advisory lock on the digest
and builds, and the rest find it finished. Cloning is not serialised by this
package at all -- PostgreSQL handles concurrent copies of one source itself.

Connections to a finished template are turned off (`ALLOW_CONNECTIONS false`),
because the one failure mode of the whole mechanism is PostgreSQL refusing to
copy a database something is attached to. To look inside one:

```text
ALTER DATABASE data_shape_abc123 ALLOW_CONNECTIONS true
```
