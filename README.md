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

## Status

Pre-release scaffold. The design lives in the plan; the vocabulary lands in
0.1.0.

## Install

```bash
pip install django-data-shape[postgres]
```

## License

MIT
