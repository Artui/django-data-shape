# From pytest

Two fixtures and one protocol. They live in `django_data_shape.fixtures` rather
than at the top level, because importing them is what requires pytest and the
rest of the package does not:

```python
from django_data_shape.fixtures import scale_fixture, shape_fixture
```

`pip install 'django-data-shape[pytest]'` pulls pytest and pytest-django in. It
composes with pytest-django rather than replacing it: your tests keep using
`django_db`, `django_assert_num_queries` and everything else, and this adds the
database they run against.

## One world for the whole session

Building a hundred thousand rows once per test is not a test suite. `shape_fixture`
builds a shape once and hands the same world to every test that asks for it.

```python
# conftest.py
import datetime

from django_data_shape import Sequential, Shape, Skew, Table, Uniform
from django_data_shape.fixtures import shape_fixture

from myapp.models import Order

orders = shape_fixture(
    Shape(
        Table(
            Order,
            rows=100_000,
            status=Skew({"complete": 0.98, "pending": 0.015, "cancelled": 0.005}),
            total=Uniform(0, 500, places=2),
            created_at=Sequential(
                datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
                datetime.timedelta(seconds=3),
            ),
        ),
        seed=1234,
    )
)
```

The name you bind it to is the fixture's name:

```python
import pytest

from myapp.models import Order


@pytest.mark.django_db
def test_the_dashboard_query(orders):
    assert orders.rows == 100_000
    assert Order.objects.filter(status="pending").count() < 2_000
```

The fixture yields the [`BuildResult`](reference.md), so a test can say how big
the world it was handed is instead of counting it again.

### What it composes with, and why it is session-scoped

The fixture requests pytest-django's `django_db_setup`, which is the seam a
project overrides to decide how its test database is made, and writes through
`django_db_blocker.unblock()`. Neither is imported: they are asked for by name,
so the coupling is to two fixture names rather than to pytest-django's internals.

Session scope is not a performance choice. pytest creates higher-scoped fixtures
before lower-scoped ones, so a session-scoped build always runs before the
function-scoped `db` fixture opens the transaction that wraps a test. That is
what makes the rows committed and visible to every later test, while everything
each test writes is rolled back with that test.

### The caveat worth reading

A test marked `django_db(transaction=True)` truncates every table when it
finishes, and takes the session's rows with it. Nothing rebuilds them, so a later
test reading this fixture is measuring an empty database. Three ways out, in the
order they are usually right:

- keep transactional tests off the tables a shape owns;
- mark them `django_db(transaction=True, serialized_rollback=True)`;
- build per test with `scaled_world(shape, 1)`, which undoes itself and
  therefore does not care.

### A scaled world can sit over a session world

A session world holds its rows for the whole run, and a scaled world is built
from empty every time. They can still point at one model: a scaled world empties
its declared tables inside the transaction it rolls back, so inside the block
those tables hold only its own world, and the session rows are back after it.
That is the shape a first consumer arrives with -- a big session world for plan
assertions, small scaled worlds for growth assertions over the same flow.

A declared table with `Disjoint` keys -- a strategy that says its keys cannot
collide with rows already there, as `UuidKeys` and `Md5Keys` do -- is the
exception, because a world leaves it alone and builds beside its rows. A
strategy implementing the protocol and answering no is emptied like any other,
which is what `build()` reads too. When it holds rows and has a foreign key
into a declared table the world empties -- directly, or through another such
table -- it is emptied too, because its rows are declared rows and cannot
outlive the parents they point at. That join reads the models' foreign keys,
not the database's, so a `ForeignKey(db_constraint=False)` pulls a table in,
and a row referencing that table is then refused where the database's own keys
would have left nothing to refuse.

A `Disjoint` table pointing at nothing the world empties -- a UUID-keyed root,
or every table of a graph keyed by UUIDs throughout -- keeps its rows, and over
a session world declaring it those rows are the session's. The world leaves them
untouched, builds beside them, and its keys are not theirs. `UuidKeys` and
`Md5Keys` make each key from the row and a stream derived from the shape's seed,
and scaling keeps the seed, so every `Disjoint` table a world builds is handed a
stream of the world's own rather than the one `build()` uses for the same seed
and table. Inside a world those keys are therefore not the ones `build()`
gives the same declaration; they are the same at every factor, so row *i* has
one key however large the world, as an integer key does; and a world opened
inside another over the same table draws from another stream again. A table
fanned out over such a table draws parents from the session's rows as well as
the world's. Over the same graph, then, a session world needs no arrangement.

A scaled world removes the rows of its declared tables **and nothing else**:
no statement it issues changes a table its shape does not declare, even for the
life of the block. Where a row it did not make references a row it would have to
remove -- a session table the scaled shape leaves out, or a row the test wrote
-- it refuses before removing anything, rather than leave the reference pointing
at nothing:

```text
A scaled world cannot empty testapp_company without changing a table its shape
does not declare: rows it did not make reference the rows it would remove
(testapp_session.company_id -> testapp_company). ...
```

The refusal is `ShapeReferenced`, and its message names each reference and the
ways out: declare the referencing table in the scaled shape too, so its rows are
the world's, or do not create those rows in that test. Where every declared
table it names can take them, it offers a third: give those tables `Disjoint`
keys (`UuidKeys` or `Md5Keys`), so the world builds beside the rows already
there instead of emptying them. Both strategies make UUIDs, so that one is
offered only where the primary key accepts a UUID -- a `UUIDField`, or a text
column with room for one -- and never for an integer key, a primary key that is
itself a foreign key, a projected table, or a table whose keys are `Disjoint`
already. Nor is it offered for a table with a foreign key into another table the
world empties, since with `Disjoint` keys that key would pull it back into the
emptying and the refusal would come back unchanged.

A foreign key left null is not a reference, and only a foreign key the database
enforces is seen: a `ForeignKey(db_constraint=False)` or a `GenericForeignKey`
is invisible to the refusal, so its row is left pointing at whatever the world
puts under that key. The refusal is made on every backend, from PostgreSQL's
catalogue there and Django's introspection elsewhere. The two read a composite
foreign key differently -- off PostgreSQL it counts when any of its columns is
set rather than all of them -- but Django never creates one.

What is still refused is a second *build* over rows that stay: two
session-scoped `shape_fixture`s over one model, or `build()` called directly over
a session world's table. The second one meets the first one's rows:

```text
testapp_order already holds rows, and this package assigns primary keys from 1,
so building over them would collide. If nothing in the test wrote them, the
usual cause is a world that was already there: a session-scoped shape_fixture
over this model holds its rows for the whole run, so a second build over it --
another shape_fixture, or build() called directly -- meets them; and a template
started from a base database keeps the rows the base holds, apart from the
tables it rebuilds for apps without migrations. Build the second world inside
scaled_world, which empties the declared tables and puts them back; give the two
different models; or empty this table first, in the base if that is where the
rows came from.
```

Give the two different models, or make the second one a scaled world.

### And it is there for tests that never asked for it

The rows are committed once, outside any test's transaction, so **every test in
the run sees them** — not only the ones that request the fixture. An ordinary
per-test fixture over the same model therefore does not start from an empty
table:

```python
# somewhere else entirely, in a file that has never heard of this package
@pytest.fixture
def an_order(db):
    return Order.objects.create(status="pending")


def test_the_dashboard_lists_it(an_order):
    assert Order.objects.count() == 1  # 100_001
    assert dashboard()["rows"] == [an_order]  # and now it has 100_000 friends
```

Nothing is wrong at the database: both sets of rows are real and correct, and the
failing assertion is the *other* test's belief about how empty the world is. That
is what makes it unpleasant to trace. **It appears only when the suite runs
together**, because running that file alone never instantiates the session
fixture — so the test passes in isolation, fails in a full run, and does so in
files that never mention `shape_fixture`.

Three ways out, in the order they are usually right:

- **give a session world models nothing else uses.** A session world owns its
  tables for the whole run;
- scope the other test's assertions rather than counting the table —
  `filter(...)` on something the shape does not produce, or assert against
  `an_order.pk` rather than a count;
- build that model per test with `scaled_world(shape, 1)` instead, which undoes
  itself.

It is documented rather than detected, and that is a limit rather than a
preference. There is no error to raise: this package cannot see the other test,
and the only mechanism that could — intercepting writes to a model some shape
owns — is a per-row hook, which is the one thing this library refuses to have at
all. Where a collision *is* an error the message says so, which is the refusal in
the section above.

## Growth: the same world at several sizes

A query count that is `O(1)` rather than `O(N)` is not something one database can
show you. You need the same code run against the same world at two sizes, which
is what the **scale protocol** is:

> make the world be at factor F, then let me run my block.

`scale_fixture` is the pytest face of it.

```python
# conftest.py
from django_data_shape import Constant, Shape, Table
from django_data_shape.fixtures import scale_fixture

from myapp.models import Order

world = scale_fixture(Shape(Table(Order, rows=100, status=Constant("complete")), seed=1234))
```

```python
def test_the_dashboard_query_does_not_grow(world, django_assert_num_queries):
    for factor in (1, 10):
        with world(factor) as rows:
            print(f"{rows} rows in the world")
            with django_assert_num_queries(3):
                dashboard()
```

No `django_db` marker: the fixture requests `db` itself, so a test that asks for
worlds has database access and an enclosing transaction without having to
remember either. Each world is built inside that transaction and undone by
rolling back to a savepoint, so the next factor starts from an empty table and
the test's own transaction survives.

A world first empties the tables its shape declares that hold rows, inside
that same transaction, and changes no other table: rows the test wrote in a
declared table are gone inside the block and back after it, and a row the test
wrote that *references* a declared table is refused, as above, rather than
emptied or orphaned. That holds under a database-level `ON DELETE` as well, such
as the one Django 6.1's `DB_CASCADE` creates: a `DELETE` follows such a key to
the rows referencing the ones it removes, and by the time one runs, the refusal
has established that none of those is outside the tables being emptied. On
PostgreSQL the emptying is one `TRUNCATE` listing the
declared tables and every table that references them, when none of those holds
rows, and otherwise a `DELETE` per declared table holding rows. Never
`TRUNCATE ... CASCADE`, which follows foreign keys by schema rather than by row:
through a chain of keys leading from a declared child back to its own parent, it
emptied the parent too.
Before the `TRUNCATE`, the foreign-key checks Django leaves deferred on the
test's own writes are fired, because PostgreSQL refuses to truncate a table with
checks still pending. A row that genuinely breaks a constraint therefore raises
`IntegrityError` on the way into the world, naming the constraint, rather than
at the end of the test. After the `DELETE`s they are fired too, so a row outside
the declaration that breaks a constraint raises on the way in on either route.
The build fires them again before the `ALTER TABLE ... SET STATISTICS` a table
declaring `statistics=` is built with, because PostgreSQL refuses it while checks
are pending: each row a `DELETE` removes from a referenced table queues one, and
a world that empties nothing leaves the test's own queued.

The two routes part on a row in a declared table that breaks a constraint, such
as an orphan the test wrote. The `TRUNCATE` route checks it before the
statement and raises `IntegrityError`. The `DELETE` route removes the row
before it fires the checks, PostgreSQL skips a check whose row is gone, and the
world builds; the orphan is back after the block.

Firing the checks ends with `SET CONSTRAINTS ALL DEFERRED`, on either route, so
from there until the block ends every deferrable constraint is deferred -- one
declared `INITIALLY IMMEDIATE` included -- and the test's own code inside the
block runs under that. The mode is transaction state, so the rollback that ends
the block restores the test's.

A `DELETE` fires row-level `DELETE` triggers, which `TRUNCATE` does not. A
trigger on a declared table runs inside the world: an audit trigger writing into
an undeclared table writes there, and the write is rolled back with the block;
a `BEFORE DELETE` trigger that returns null keeps its rows, and the build then
refuses the table with `ShapeNotEmpty`.

Outside a fixture, the same thing is a context manager:

```python
from django_data_shape import scaled_world

with scaled_world(shape, 10) as rows:
    ...
```

### Declare small, scale up

The declared row counts are the world at factor 1, so the base declaration should
be **the smallest world that still means something**. A hundred rows against a
thousand is the regime this is for, and it is milliseconds per factor.

Size, in the two-million-row sense that makes a query *plan* realistic, is a
different assertion with a different cost -- and it does not vary a factor at
all. Growth is about the shape of the count curve; plans are about the planner.

### Where to open a query capture

Inside the block, never around it. Building a world emits statements of its own,
and a capture wrapped around `world(factor)` counts them with the block's:

```python
def test_the_dashboard_query_does_not_grow(world, django_assert_num_queries):
    for factor in (1, 10):
        with world(factor):
            with django_assert_num_queries(3):  # inside, not outside
                dashboard()
```

On PostgreSQL the hazard is mild and fixed -- nineteen statements for a
two-table shape over empty tables, at every factor, because one `COPY` loads a
table however many rows it carries, and everything else a world emits -- the
read of which declared tables hold rows, the emptiness check, the
statistics-target read, the parent key read, the sequence resets, the `ANALYZE`
and the savepoints -- is counted per table or per world, never per row. Over a
session world declaring the same tables, emptying them adds five more where
another table references them, four where none does, the same at every factor.
Off PostgreSQL it is neither mild nor fixed: the inserts are
ordinary statements, one per thousand rows, so the count a capture sees **grows
with the factor**, and a growth assertion measuring from outside the block would
read the loader's curve as its subject's.

Both halves are pinned by tests in this package's own suite rather than left as
prose. The number above was already wrong once, and the consumer it matters to
cannot check it without taking the dependency the protocol exists to avoid.

### Varying one dimension

`scaled_shape` multiplies every table, so a single factor moves the parents and
the children together. That is on purpose -- a child-only factor changes the
average fan-out along with the size -- but it means the curve shows growth
without naming which axis caused it.

There is no per-table factor and no `scale=` flag, because none is needed: the
protocol takes a **callable**, so which dimension varies is a property of the
function you bind rather than of the seam.

```python
import contextlib

from django_data_shape import Constant, FanOut, Shape, Table, Zipf, scaled_world


@contextlib.contextmanager
def more_customers(n, /):
    # Orders pinned, customers growing. Note what this really asks for: with the
    # child count fixed, more parents means fewer children each, so the fan-out
    # moves too. That is the honest reading of "O(parents)", and a flag that
    # hid it behind a boolean would be hiding the confound rather than the
    # arithmetic.
    shape = Shape(
        Table(Customer, rows=50 * n, name=Constant("acme")),
        Table(Order, rows=5_000, customer=FanOut(Zipf()), status=Constant("complete")),
    )
    with scaled_world(shape, 1) as rows:
        yield rows
```

### Why a factor varies the declaration

The alternative was to build once at the largest factor and let a smaller factor
see only part of it. It does not survive contact with what a subset actually is:

- **A subset is not a smaller database; it is the same database with a filter.**
  The table still holds every row, the statistics still describe every row, and
  an index still spans every row. Worse, the block under test would have to
  cooperate by restricting itself to the subset -- so the harness would leak into
  the code being measured, and a growth assertion whose subject knows it is being
  scaled is measuring the harness.
- **A fan-out is a partition of the child key range**, so cutting the children
  short changes the shape rather than the size: it removes whole parents under
  `grouped` placement and thins every parent under `arrival`. The childless share
  and the tail are the two things the declaration exists to state, and they would
  come out different at every factor.
- **A shape is inert, hashable data, and a scaled shape is another one.** That is
  the representation the template-database cache will key on, so each factor gets
  a cache key for free. A subset has no key of its own.

Every table scales, parents included. Scaling only the child table would change
the average fan-out along with the size, so two worlds would differ in a second
way and the curve would no longer be about size.

`scaled_shape` is that transform on its own, and it needs no database:

```python
from django_data_shape import scaled_shape

bigger = scaled_shape(shape, 10)
```

### Implementing the protocol without this package

A consumer of the protocol -- a growth assertion in another library, say --
depends on the *shape* of the call and not on this package. Anything callable as
`at(factor)` returning a context manager will do, so a project on a backend this
package refuses supplies its own:

```python
import contextlib


@contextlib.contextmanager
def world(n):
    orders = [make_order() for _ in range(100 * n)]
    try:
        yield len(orders)
    finally:
        delete(orders)
```

The factor is **positional-only** in the protocol, so an implementation may call
it whatever reads best -- `n` above. That is deliberate: a structural type
matches parameter names too, so without it the protocol would have accepted only
implementations that happened to spell the argument `factor`, which is a rule
about this package's naming rather than about the shape of the call.

Restated without importing `ScaleProtocol`, for a consumer who would rather spell
the shape than depend on it, it is exactly:

```python
Callable[[int], AbstractContextManager[int | None]]
```

The value yielded is how many rows the world holds, **or `None`**. It is a
diagnostic: the growth curve's x-axis is the factor, which the caller passed in
and already knows. That is also why it is a plain number rather than a
`BuildResult` -- a seam a stranger cannot implement is not a seam -- and why
`None` is allowed, because the shortest honest implementation of this protocol
builds rows and has no count to hand back:

```python
@contextlib.contextmanager
def world(n):
    build_my_fixtures(100 * n)
    yield
```

A caller reading the value has to tolerate `None`. An implementation that can
count cheaply should still yield the number.

For a shape with more than one table that number is the **sum across tables**,
which is a total rather than an axis: a world of 100 companies and 1,000 orders
yields 1,100, and nothing in the protocol says which of the two grew. The factor
is the axis; the sum is for a message a human reads. `BuildResult.tables`, which
does break the total down, is not reachable through the protocol -- deliberately,
since it is one of this package's own types.

## On SQLite

**Growth works. Plans do not.** Which of the two fixtures you asked for is what
decides, and the split is the package's own line drawn where it belongs:
generation and cardinality are backend-neutral, planner realism is not.

`scale_fixture` and `scaled_world` build on any backend Django supports. A
growth assertion counts queries, and a query count is an ORM property that means
the same everywhere. Where the backend has `COPY` and column statistics they are
used; where it does not, the rows are inserted and nothing is analyzed -- so the
cardinality is real and no plan is claimed. SQLite has an `ANALYZE` of its own
and it is deliberately not run, because running it would be this package
claiming, in the only way a library can, that the plan over those rows means
something.

The cost is not what makes the decision either way. Measured on SQLite, the
insert is about 1.6 ms per thousand rows against 8 ms to generate them, so at the
scales a growth assertion runs at the load is not what you are paying for.

`shape_fixture` skips, with the refusal as the stated reason:

```text
SKIPPED [1] test_orders.py:14: Building a shape for the test session needs
PostgreSQL; connection 'default' is sqlite. Generation and cardinality are
backend-neutral, but COPY loading and planner statistics are not, and a shaped
database whose plans mean nothing is worse than no shaped database at all.
```

That is the fixture whose job is to be big and to be believed by a planner, and
that world cannot exist here. A skip is the honest degradation: a test that never
ran says so, while a test that ran against a database nobody shaped passes and
means nothing. If you are writing your own fixture over a shaped database --
anything that asserts on a *plan* -- `skip_unless_postgres` is the same behaviour
to reach for:

```python
import pytest
from django.db import connections

from django_data_shape.fixtures import skip_unless_postgres


@pytest.fixture
def my_own_world(db):
    skip_unless_postgres(connections["default"], "Measuring a plan")
    ...
```
