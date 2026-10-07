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
[`Table`][django_data_shape.declaration.table.Table] and on
[`Projection`][django_data_shape.declaration.projection.Projection] alike. A column left out
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

[`template_database`][django_data_shape.databases.template_database.template_database]
names the database after a content hash of everything that decides what is in it,
so reuse is safe rather than merely fast. Change the declaration, the schema, the
time-zone settings, this package's version or the base it starts from and the
name changes, so the old database is simply never asked for again.

### The key

[`shape_digest`][django_data_shape.databases.shape_digest.shape_digest] is the declaration
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
[`UnhashableShape`][django_data_shape.databases.unhashable_shape.UnhashableShape], naming
the table and the column. Build it with `build()` and pay the load, or implement
[`Canonical`][django_data_shape.types.canonical.Canonical] on a declaration that really
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

### Starting from a migrated base

By default a template is built into a database that `migrate` fills from empty,
and on a project with a long migration history that replay is most of the cost: one with
about 330 migrations measured thirteen and a half minutes of `migrate` against
about one minute to build a four-million-row shape -- paid again by every
template a change to the declaration makes. Such a project usually keeps a
migrated database already, and clones its test databases from it. Name it as
the base and the template starts as a copy of it instead:

```python
# conftest.py
import pytest
from django.db import connections

from django_data_shape import clone_database, drop_database, template_database

BASE = "myproject_base"  # a database you keep migrated


@pytest.fixture(scope="session")
def django_db_setup(django_test_environment, django_db_modify_db_settings, django_db_blocker):
    connection = connections["default"]
    settings = connection.settings_dict
    target = settings["TEST"]["NAME"] or f"test_{settings['NAME']}"

    with django_db_blocker.unblock():
        clone_database(template_database(SHAPE, base=BASE), target, replace=True)
        connection.close()
        settings["NAME"] = target
        yield
        connection.close()
        drop_database(target)
```

The tables `migrate` builds for apps without migrations are rebuilt from the
models, and everything else the base holds is carried as it is. An app with
migrations is brought forward by its history: `migrate` runs over the copy and
applies whatever the base has yet to -- nothing, on a base kept up to date. An
app without migrations has no history to bring forward, so the tables
`run_syncdb` makes for it -- its managed models' tables, and the many-to-many
tables Django creates for their relations -- are dropped from the copy first
and made again from the current models, as in an empty database. `run_syncdb`
creates a missing table and never alters an existing one, so this is what stops
a table the base made from an older model surviving under a key that names the
new one -- and it means the base's rows in those tables do not carry over.
Everything else is carried as the base has it: the rows in the tables of apps
with migrations, an unmanaged model's table and the tables of an app that is no
longer installed. `post_migrate` fires as it does from empty.

**A base behind the migrations on disk is migrated forward; only a history
migrating cannot repair is refused.** Migrating forward always ends at the
checkout's schema for the apps with migrations, whatever prefix of their history
the base holds, and the apps without them are rebuilt, so the key stays sound as
it is:
everything it absorbed before, plus the base's name and oid. The base's applied
migrations do not need to enter it. Migrating the base itself forward later
leaves its oid alone, so the same name is asked for, and the template under it is
the one the copy's own forward migration already gave. Django's own PostgreSQL
`TEST: {"TEMPLATE": ...}` setting behaves the same way. A base far behind pays its
`migrate` once per key, which is never more than building from empty pays -- and
accepting it is what keeps the base a project has most often, one migration
behind because a branch added one, from being refused at all.

What is refused is what migrating cannot fix, raising
[`UnusableBase`][django_data_shape.databases.unusable_base.UnusableBase] before
anything is created, with the one exception marked, and naming the base:

- **ahead** -- the base records an applied migration of an installed app that no
  migration on disk is or replaces. Of the histories a base can record, this is
  the one migrating cannot bring to the checkout's schema: it moves only
  forward, so whatever those migrations did stays in the copy, and a branch
  migration that adds only an index would otherwise build silently and skew plan
  assertions. The
  message names up to three of them and gives two remedies, because the two
  usual causes need opposite ones. Rows left behind by squashed migrations whose
  files were deleted after the squash's `replaces` was removed are pruned with
  `manage.py migrate <app> --prune` against the base (Django 4.1 and later). A
  migration from another branch is undone by migrating the base back from a
  checkout that has it, or by recreating the base -- pruning its row would leave
  its schema in place and only silence the refusal. When the app has a squash
  on disk that still lists what it replaces, the message says so, because
  `--prune` declines to run while one does: finish the squash first, by running
  `migrate` against the base so that it is recorded as applied and then
  removing its `replaces`, which makes it an ordinary migration, and prune
  after.
- **a squash applied in part, with its replaced files deleted** -- the base
  records some of the migrations a squash on disk replaces, and one it has yet
  to apply is no longer on disk. Django runs a squash only when all or none of
  what it replaces is applied, and otherwise runs the replaced migrations
  themselves, so with one of those files gone neither runs and the template
  would silently lack what they do. Migrate the base from a checkout that
  still has the replaced migrations, or recreate it. A squash applied in part
  whose replaced files are still there is not refused: Django finishes it one
  replaced migration at a time. From Django 6.0 a squash can replace another
  squash, and Django judges "in part" over everything the two come down to, so
  this is read off the plan Django actually made rather than off what the outer
  squash lists: a squash of a squash is refused when the base applied some of
  what the inner one replaces and a migration after it is gone from disk.
- **tables with no record of their migrations** -- the base holds the tables of
  an app with migrations and records no applied migration for that app, so
  `migrate` would create them again and fail on the first. It is judged app by
  app. A base with no `django_migrations` table is the whole-database form; a
  base restored from `pg_dump --schema-only` is the commonest, because the
  table comes back with none of its rows; and a base made while an app had no
  migrations is the one-app form, once the app gains them. Restore the rows of
  `django_migrations` with the schema, or recreate the base with `migrate`. An
  empty database holds none of those tables and is migrated in full; the tables
  of an app without migrations do not count, because `run_syncdb` records
  nothing for them.
- **a reference into a rebuilt table** -- something in the base outside the
  tables being rebuilt depends on one of them: a foreign key or a view. Those tables are dropped in one statement without `CASCADE`,
  because `CASCADE` would remove the reference silently, `run_syncdb` would not
  put it back, and the template would match neither the base nor one built from
  empty. So PostgreSQL refuses the drop, and the refusal is raised as
  `UnusableBase` naming the base and the object in the way. This one is raised
  from the copy rather than before it, since only the copy can find it, and the
  partial is dropped with it. Drop the reference in the base or recreate it
  without one. Django does let a migration of an app with migrations point at
  an app without them, and in that project no base can be started from until
  the app pointed at has migrations of its own; build from empty instead.
- **missing** -- no database by that name exists.
- **closed** -- the database does not accept connections (`ALLOW_CONNECTIONS
  false`). A base is connected to before it is copied, to read which migrations
  it has applied.
- **a template** -- a database named `data_shape_` and a sixteen-character
  digest, with or without the `__partial` suffix a build works under, which
  this package made. A template holds a shape's rows under a name keyed for
  that shape, so it is never a base, whether or not connections to it have been
  turned back on. A database of your own that merely starts with `data_shape_`
  is a base like any other.
- **a double quote in the name** -- Django quotes a database name by wrapping it
  in double quotes, passes one that is already wrapped through unchanged and
  escapes nothing inside it, so the database checked and the database copied
  could be two different ones.

Two kinds of row are not ahead. A squash whose replaced files were deleted
leaves their rows behind, and they are not ahead while the squash still lists
them in its `replaces` -- once all of them are applied; a squash applied in part
is the case above. And a row for an app that is no longer installed
describes nothing the checkout's models use, so it is ignored; its tables, if
any are left, are carried like any other table the shape does not declare. The
rows a squash of a squash leaves are a different matter: once only the outer
squash is on disk, the migrations the inner one replaced are listed by nothing
on disk, so their rows read as ahead although Django counts the base as
migrated. That is the case the ahead message's advice to finish the squash
first is for.

The base is checked on every call, a cache hit included, because its oid is part
of the key. That is also what makes refreshing a base safe: dropping and
recreating it, which is how one restored from a dump is usually updated, gives
it a new oid and so a new template. The dump has to carry the rows of
`django_migrations` as well as the schema; `pg_dump --schema-only` alone
restores a base that is refused, as above.

Whatever else the base holds, outside the tables being rebuilt, becomes
template content, which is what lets the base carry reference data the
shape does not declare. Rows in a table the shape
*does* declare are refused by the build's emptiness check as they would be
anywhere, with a `ShapeNotEmpty` message that names the base as one place they
come from, except in a table with `Disjoint` keys, which is exempt from it.

The base must have nothing attached to it while the template is copied -- the
same rule as for cloning a template, and for the same reason. This process's own
connection is closed first, so a project whose test database is the base can
pass it as one.

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
  [`drop_database`][django_data_shape.databases.drop_database.drop_database] removes one.
- **A `RunSQL` edited inside a migration that already exists.** The key covers
  every migration's name and every model's fields, so ordinary schema changes
  move it; editing the body of a migration that has already been created changes
  neither. Drop the template by hand when that happens.
- **A migration regenerated under a name a base has already applied.** With a
  base, an edited migration goes one step further, and it is a case nothing can
  detect: `migrate` reads the name as applied and skips it, so
  the copy keeps the version the base ran, and a template rebuilt from the same
  base would keep it again. Recreate the base, which gives it a new oid and so a
  new template.
- **What a migration did to a rebuilt table.** A migration of an app with
  migrations can run SQL against a table of an app without them -- an index, a
  trigger, a policy, a grant or a comment. A base has applied it, so when the
  copy rebuilds that table what the migration made is lost, and the migration is
  never run again; nothing in the copy says which index or trigger came from
  where, so this is stated rather than detected. Recreating the base does not
  help, because a recreated base has applied the migration too. Build that
  template from empty, with `base=None`: there `run_syncdb` makes the table
  before the migration runs.
- **Rows changed in a base in place.** The key covers a base's name and oid,
  and the schema any accepted base migrates forward to; changing the rows it
  holds, by hand or by migrating the base, neither of which moves its name or
  oid, changes none of them, so the template built from the old rows is still
  the one asked for. Drop it with `drop_database` when that happens, or
  recreate the base rather than changing it.
- **A base ahead of the migrations on disk.** It is refused with the remedies,
  as above, rather than migrated back: the migrations that would undo it are not
  in this checkout.

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
