# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- The shape vocabulary: `Shape`, `Table`, and the `Skew`, `Uniform`, `Sequential`
  and `Constant` distributions, behind a single-method `Distribution` protocol.
- `build()`, which generates rows, loads them with `COPY FROM STDIN`, moves the
  identity sequence past the keys it assigned, and runs `ANALYZE`. The order is
  owned by the library: loading into a table analyzed while empty leaves the
  planner applying old statistics to a new row count, which is a worse lie than
  having no statistics at all.
- `InvalidShape`, raised at declaration time, and `UnsupportedBackend`, raised
  for any connection that is not PostgreSQL. Generation is backend-neutral;
  `COPY` and planner statistics are not, and degrading quietly would produce the
  false confidence this package exists to remove.
- Primary keys are assigned as a dense `1..N` range rather than declared. That is
  what will let a foreign key be satisfied without a lookup once relations land,
  and what makes a self-referential tree acyclic by construction.
- A field carrying a Django `default=` is filled with the value `save()` would
  have written, because defaults are applied by `save()` and nothing here calls
  it. A callable default is refused instead of guessed: `uuid4` varies per row
  and `dict` does not, and nothing on the field distinguishes them.

### Notes
- Relations are refused rather than approximated. Fan-out as a distribution is
  the next release, and generating a foreign key from a value distribution would
  write ids pointing at rows that may not exist.

[Unreleased]: https://github.com/Artui/django-data-shape/compare/v0.0.0...HEAD
