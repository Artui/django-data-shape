# django-data-shape

[![CI](https://github.com/Artui/django-data-shape/workflows/tests/badge.svg)](https://github.com/Artui/django-data-shape/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/django-data-shape.svg)](https://pypi.org/project/django-data-shape/)
[![Python versions](https://img.shields.io/pypi/pyversions/django-data-shape.svg)](https://pypi.org/project/django-data-shape/)
[![Django versions](https://img.shields.io/pypi/djversions/django-data-shape.svg)](https://pypi.org/project/django-data-shape/)
[![Docs](https://img.shields.io/badge/docs-artui.github.io-blue.svg)](https://artui.github.io/django-data-shape/)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/Artui/django-data-shape/gh-pages/coverage.json)](https://github.com/Artui/django-data-shape/actions/workflows/tests.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License](https://img.shields.io/pypi/l/django-data-shape.svg)](LICENSE)

A realistically shaped test database from Django models.

Declare the shape of your data -- cardinality, value skew, foreign-key fan-out as
a distribution with a long tail, and where related rows physically sit -- then
load it by `COPY` and `ANALYZE` it, so the query planner makes the same choices
it will make in production.

It exists because a plan over ten rows is a lie, and because the loop it replaces
is not merely smaller: uniform fan-out makes the planner always right, and
generating children parent-by-parent clusters them perfectly, which flatters
every index scan. A test database can be wrong in the flattering direction, and
usually is.

## Install

```bash
pip install 'django-data-shape[postgres]'
```

The quotes are not decoration: zsh globs the brackets and reports
`no matches found` without them.

## Use

```python
import datetime

from django_data_shape import Sequential, Shape, Skew, Table, Uniform, build

from myapp.models import Order

shape = Shape(
    Table(
        Order,
        rows=1_000_000,
        status=Skew({"complete": 0.98, "pending": 0.015, "cancelled": 0.005}),
        total=Uniform(0, 500, places=2),
        created_at=Sequential(
            datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
            datetime.timedelta(seconds=3),
        ),
    ),
    seed=1234,
)

build(shape)
```

`build()` generates the rows, loads them with `COPY`, moves the identity sequence
past the keys it assigned, and runs `ANALYZE` so the planner can see the shape.
It raises on any backend that is not PostgreSQL rather than degrading quietly --
unless you say `require_statistics=False`, which asks for rows and cardinality
instead of a database the planner can reason about, and is what the growth
harness below is built on.

## Relations

```python
from django_data_shape import Constant, FanOut, Shape, Table, Zipf, build

build(
    Shape(
        Table(Company, rows=50, name=Constant("acme")),
        Table(
            Order,
            rows=2_000_000,
            # A distribution, not a number: giving every parent ten children is
            # the one shape in which the planner is never wrong, because its
            # n_distinct average is then the truth.
            company=FanOut(Zipf(1.2), childless=0.35),
            status=Constant("complete"),
        ),
    )
)
```

The parents can be rows this package built or rows your own code did -- their
real keys are read, not assumed, so the ORM can own the small tables while this
owns the large ones.

## Derivations

A distribution says what a column looks like **across** rows. A derivation says
what one column is **given** the others -- and the four faces are one mechanism,
differing only in where the inputs are read from.

```python
from django_data_shape import After, Aligned, Derived, FanOut, Given, Table, Zipf

Table(
    Ticket,
    rows=2_000_000,
    account=FanOut(Zipf(1.2)),
    # the parent row: this ticket's own account, read across the fan-out
    opened_at=After("account.signed_up_at", within=timedelta(days=365)),
    severity=Given("account.plan", {"free": mostly_low, "enterprise": mostly_high}),
    # a shared rank: the big tickets are big in both columns at once
    quantity=Aligned("size", Uniform(1, 100, places=0)),
    unit_price=Aligned("size", Uniform(1, 500, places=2)),
    # this row
    total=Derived("quantity", "unit_price", compute=operator.mul),
)
```

`Derived` is the mechanism and takes `scope=` directly, so a correlation nobody
shipped a face for is still declarable. Column order is the `COPY` column list
and says nothing about dependencies, so derivations get a computation order of
their own and a cycle among them is refused by name.

**This package may call your code, and your code may not call the database.**
Generation runs under a wrapper on the connection being built, so a query raises
rather than quietly costing a round trip per row. A per-row creation hook is the
thing this package replaces, and a hook whose body may query is a hook whose body
will.

## Projections

Some tables are copied rather than distributed. An `Event` is created from a
`Template`, and its `EventSession` rows mirror that template's `TemplateSession`
rows -- so the child count is *determined*, and correlated with the template.

```python
Shape(
    Table(Template, rows=500, name=Constant("t")),
    Table(TemplateSession, rows=4_000, template=FanOut(Zipf()), title=Constant("s")),
    Table(Event, rows=200_000, template=FanOut(Zipf()), name=Constant("e")),
    Projection(EventSession, per=Event, copying=TemplateSession),
)
```

One `INSERT ... SELECT`, derived from the model graph, with raw SQL as the escape
hatch. There is no `rows=`: the count comes from the join, and comes back in the
`BuildResult`. It is what a creation service collapses into at scale -- one event
from a template is a service call, a million is one statement -- and it
reproduces a correlation a `FanOut` on the child would destroy.

## Statistics, and building once

`ANALYZE` runs at the end of every build, because rows the planner cannot see are
worse than no rows. How much of a column it records is decided by that column's
**statistics target**, so a declaration wider than the target is one PostgreSQL
would build and then not see -- and this refuses rather than producing it:

```python
Shape(
    Table(
        Event,
        rows=2_000_000,
        kind=Skew(weights),  # 150 event types
        statistics={"kind": 300},  # the planner keeps 100 unless asked
    )
)
```

The target is declared, never inferred: it is a property of the column rather
than of the distribution, and a package choosing one for you would be deciding
how the planner sees your data on evidence your declaration does not contain.
What the distributions are read for is the refusal.

Building that database costs about seventeen seconds. Copying it costs about two
hundred milliseconds, statistics included:

```python
from django_data_shape import clone_database, template_database

template = template_database(shape)  # builds the first time, finds it after
clone_database(template, "test_myapp", replace=True)
```

The template is named after a content hash of the declaration, the schema, the
relevant settings and this package's version, so a stale one is never asked for.
A shape holding a `Derived` or a `KeyFunction` is refused rather than hashed --
there is no honest digest of a callable, and every way of guessing one agrees
while the data has changed. See
[Statistics and reuse](https://artui.github.io/django-data-shape/statistics/).

## From pytest

```python
# conftest.py
from django_data_shape import Constant, Shape, Table
from django_data_shape.fixtures import scale_fixture, shape_fixture

orders = shape_fixture(Shape(Table(Order, rows=100_000, status=Constant("complete"))))
world = scale_fixture(Shape(Table(Order, rows=100, status=Constant("complete"))))
```

`orders` is one world built once for the whole session, composed with
pytest-django rather than replacing it. `world` is the **scale protocol**: make
the world be at factor F, then let the caller run its block, which is what a
query count asserted to be `O(1)` rather than `O(N)` needs.

```python
def test_the_dashboard_does_not_grow(world, django_assert_num_queries):
    for factor in (1, 10):
        with world(factor):
            with django_assert_num_queries(3):
                dashboard()
```

A factor varies the declaration rather than subsetting one larger build, and
`pip install 'django-data-shape[pytest]'` is what these two need. **The growth
harness works on any backend Django supports**, because a query count is an ORM
property and means the same everywhere; the session world **skips with a stated
reason** where a shaped database cannot exist, because a plan over it is the
thing it exists to make honest.

## What it expects, and what it refuses

A declaration that cannot describe a database raises before a row is generated,
naming the field. In particular:

- **PostgreSQL and psycopg 3.** Rows stream into `COPY FROM STDIN`, which
  psycopg 2 cannot do without materialising them first. Both are refused by name
  rather than degraded around. PostgreSQL is required for the statistics half
  only: `build(shape, require_statistics=False)` loads rows on any backend and
  claims nothing about a plan. psycopg 2 is refused either way, because the
  vendor picks the route and not the caller.
- **A key type it can assign.** Integer keys count from one and UUID keys are
  derived from the seed; anything else is refused rather than guessed, and
  `keys=KeyFunction(...)` declares one.
- **Empty tables.** Keys start at 1 on every build, so `build()` checks first and
  raises rather than colliding partway through.
- **A callable model default** such as `default=uuid4` must be declared as a
  distribution: `uuid4` varies per row and `dict` does not, and nothing on the
  field distinguishes them.
- **A derivation that queries the database.** The generation pass is guarded, so
  the rule is a refusal rather than advice. Read what you need before the build
  and close over it.

## Status

Early. Single tables, the model graph, the pytest surface, the derivation
mechanism, projections, statistics targets and template-database reuse.
Per-group invariants and many-to-many edges come next.

Full documentation: <https://artui.github.io/django-data-shape/>

## License

MIT
