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
It raises on any backend that is not PostgreSQL rather than degrading quietly.

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
`pip install 'django-data-shape[pytest]'` is what these two need. On a backend
that cannot carry a shaped database both **skip with a stated reason** rather
than passing over one nobody shaped.

## What it expects, and what it refuses

A declaration that cannot describe a database raises before a row is generated,
naming the field. In particular:

- **PostgreSQL and psycopg 3.** Rows stream into `COPY FROM STDIN`, which
  psycopg 2 cannot do without materialising them first. Both are refused by name
  rather than degraded around.
- **A key type it can assign.** Integer keys count from one and UUID keys are
  derived from the seed; anything else is refused rather than guessed, and
  `keys=KeyFunction(...)` declares one.
- **Empty tables.** Keys start at 1 on every build, so `build()` checks first and
  raises rather than colliding partway through.
- **A callable model default** such as `default=uuid4` must be declared as a
  distribution: `uuid4` varies per row and `dict` does not, and nothing on the
  field distinguishes them.

## Status

Early. Single tables, the model graph and the pytest surface. Derived fields,
collections copied along a join, per-group invariants and template-database reuse
come next.

Full documentation: <https://artui.github.io/django-data-shape/>

## License

MIT
