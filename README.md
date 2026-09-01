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
pip install django-data-shape[postgres]
```

## Use

```python
import datetime

from django_data_shape import Sequential, Shape, Skew, Table, Uniform, build

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
Table(
    Order,
    rows=2_000_000,
    # A distribution, not a number: giving every parent ten children is the one
    # shape in which the planner is never wrong, because its n_distinct average
    # is then the truth.
    company=FanOut(Zipf(1.2), childless=0.35),
    ...
)
```

The parents can be rows this package built or rows your own code did -- their
real keys are read, not assumed, so the ORM can own the small tables while this
owns the large ones.

## Status

Early. Single tables and the model graph. Derived fields, collections copied
along a join, per-group invariants and template-database reuse come next.

Full documentation: <https://artui.github.io/django-data-shape/>

## License

MIT
