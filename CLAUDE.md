# CLAUDE.md

Guidance for working in this repository. The design that produced it lives in the
workspace roadmap, not here; this file is about how the code is written.

## What this package is

`django-data-shape` builds a realistically shaped test database from a
declaration over Django models: cardinality, value skew, foreign-key fan-out as a
distribution with a long tail, the physical placement of related rows, and the
business invariants that make the data possible at all. It loads by `COPY`, runs
`ANALYZE`, and caches the result as a template database.

It exists for one claim, and every design decision answers to it: **if the query
planner does not change its mind because of this library, the library did
nothing.** Row count is not shape. Statistics are, and so is placement.

Three things follow that are easy to get backwards:

- **Uniform fan-out makes the planner always right**, because the average is the
  truth. A generator that gives every parent ten children builds the one database
  in which join misestimation cannot occur.
- **Generating children parent-by-parent clusters them perfectly**, a layout no
  production table has, which flatters every index scan.
- **Rows without statistics are worse than few rows**, because the planner
  guesses a default selectivity and commits to it.

It is **not** a fixtures library. `model_bakery` makes objects and does it well;
this composes with it rather than replacing it.

## Commands

```bash
make init          # uv sync --all-groups + pre-commit install
make test          # pytest against Postgres, 100% line+branch required
make test-sqlite   # the portable half only, no coverage gate
make lint          # ruff check + ty check
make format        # ruff format
make docs-build    # mkdocs build --strict
```

`make test` needs a running Postgres. That is deliberate -- see Tests.

## Structural rules

- **One exported symbol per file**, file named in `snake_case` after it.
- Private one-file helpers are prefixed `_`; helpers used across files go in
  `utils.py`.
- **Top-level imports only.** No function-level imports.
- **Full type annotations** on every function and method.
- `__init__.py` is the only re-export point, and holds no logic.
- Every annotated module starts with `from __future__ import annotations`. Ruff's
  `required-imports` enforces this rather than leaving it to memory.

## The rules the design rests on

These are not style preferences; each one is what keeps a feature buildable.

- **A shape must be generatable in one pass, parent before child.** Every
  primitive has to be computable when a row is emitted, knowing only its parent.
  Anything needing a global solve is a constraint satisfaction problem, and a CSP
  cannot stream into `COPY`. This is the boundary that keeps the package a
  library.
- **A distribution is declared over a fixed count, never as a multiplier.** Rows
  times fan-out makes cardinality emergent and therefore unassertable. The same
  rule governs M2M edge counts and per-group invariants, where one distribution
  is *derived* from another rather than declared beside it.
- **This package owns the primary keys** -- dense, deterministic, `1..N`. That is
  what lets a child's foreign key be satisfied by construction with no lookup, and
  it is why self-referential trees are acyclic for free (`parent_id < id`). It
  also means the sequence must be reset to `N+1` after loading, or the first ORM
  `create()` in a test raises `IntegrityError`.
- **Refuse loudly rather than approximate.** A declaration that cannot be
  satisfied is rejected at declaration time, naming the field or constraint and
  the arithmetic -- never generated as a best effort.
- **Degrade honestly.** Generation and cardinality work on any backend;
  everything statistics-shaped is Postgres-only and must say so rather than
  appearing to work. A shaped database that silently gives a meaningless plan is
  worse than a skip.

## Adding a feature

Write the test first, watch it fail, then implement. A new public symbol gets its
own module, a re-export in `__init__.py`, and a docs entry.

## Tests

- `tests/` mirrors the source layout.
- `pytest-asyncio` runs in auto mode.
- **100% line and branch coverage.** Never `# pragma: no cover` -- restructure
  the code instead.
- **The default backend is Postgres, not SQLite**, and that is the package's own
  thesis applied to itself: a suite that passes because the backend could not
  check anything is exactly the failure this library exists to expose. A
  contributor without a local server gets a connection error rather than a
  misleading pass. `make test-sqlite` runs the portable half.
- **Backend-refusal paths branch on the connection's vendor, so they are
  covered by passing a vendor -- not by running the suite on the backend being
  refused.** Keep it that way: a degradation path reachable only on the backend it
  refuses cannot be covered by the job that gates coverage, and the gate would
  have to move somewhere it means less.
- A test that asserts a plan must assert the *plan*, never a duration.

## Type checking

`ty`, not mypy. `make type-check` runs it over the package.
No `# type: ignore` -- a pre-commit hook rejects it.

## Linting and formatting

Ruff is the source of truth for both. Use `...` over `pass` for empty bodies.
**`make lint` does not run `ruff format --check`; CI does.** Run
`uv run ruff format --check` before pushing.

## Imports inside the package

Absolute only. `from django_data_shape.x import y`, never `from .x import y` --
`ban-relative-imports = "all"` rejects every relative form including single-dot.

## Compatibility floor

`django>=4.2`, `python>=3.10`. The floor is a claim checked per PR by the
`lowest declared versions` CI job, which resolves `--resolution lowest-direct`
and runs the suite against that resolution. Raise a floor only when a
declaration needs a newer API -- never on age.

`psycopg[binary]>=3.2` is the `postgres` extra. psycopg 2 is deliberately
unsupported: `copy_expert` takes a file-like object, so it would mean
materialising generated rows before loading them, which is the cost this package
exists to avoid.

## CI and pre-commit

Every action is pinned to a commit SHA with a trailing `# vX.Y.Z` comment. The
comment is functional -- Dependabot parses it and rewrites both together.

Pre-commit runs gitleaks, the standard hygiene hooks, ruff, ty, and four
convention guards: no absolute local paths, no internal plan labels, no
mypy-style `# type: ignore`, no emoji or marker glyphs in code, docs or
changelogs. Never add a `--no-verify` escape hatch; fix the cause.

**The coverage gate lives on the Postgres job**, inverting the arrangement in
the sibling repos. There, Postgres-only paths were the exception; here they are
the product, so gating on the portable matrix would gate on the half that proves
least.

## Releasing

```bash
make release-bump VERSION=X.Y.Z
# edit CHANGELOG.md to fill in the new section, review the diff
# open a PR, get it reviewed, merge to main
```

Merging to `main` triggers `release.yml`, which no-ops unless the version in
source has been bumped past the most recent `vX.Y.Z` tag.
**If a step after the PyPI upload fails, re-run the job** -- every phase is
idempotent and that is the designed recovery. Do **not** hand-push the tag:
`prepare` gates on the tag, so a manual one makes it report `released=false` and
skips both the finalize step and the docs deploy.
