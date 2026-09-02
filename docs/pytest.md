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

### The two fixtures do not share a table

A session world holds its rows for the whole run. A scaled world is built from
empty every time. Point both at one model and the second one is refused:

```text
testapp_order already holds rows, and this package assigns primary keys from 1,
so building over them would collide. If nothing in the test wrote them, the
usual cause is a world that was already there: a session-scoped shape_fixture
over this model holds its rows for the whole run, and a scaled world cannot
build over them. Give the two different models, or empty this table first.
```

That is the shape a first consumer arrives with -- a big session world for plan
assertions, small scaled worlds for growth assertions -- so it is worth saying
before the message has to. **They compose over a graph by taking different
models**, not by taking turns over one: the session world gets the tables a plan
assertion needs to be big, the scale harness gets the tables a growth assertion
counts.

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

On PostgreSQL the hazard is mild and fixed -- twelve statements for a two-table
shape, because `COPY` does not pass through Django's `execute_wrapper` and only
the emptiness check, the parent key read, the sequence reset, the `ANALYZE` and
the savepoints do. Off PostgreSQL it is neither mild nor fixed: the inserts are
ordinary statements, one per thousand rows, so the count a capture sees **grows
with the factor**, and a growth assertion measuring from outside the block would
read the loader's curve as its subject's.

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

The value yielded is how many rows the world holds. It is a diagnostic: the
growth curve's x-axis is the factor, which the caller passed in and already
knows. That is also why it is a plain number rather than a `BuildResult` -- a
seam a stranger cannot implement is not a seam.

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
