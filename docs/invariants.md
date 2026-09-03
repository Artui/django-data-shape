# Invariants

A company has many projects, at most one of which may be `ACTIVE`. In Django
that is a partial `UniqueConstraint`, and it is the shape of a very large class
of real schemas.

```python
from django_data_shape import FanOut, Invariant, PerParent, Sequential, Shape, Table, Zipf, build

shape = Shape(
    Table(Company, rows=50_000, name=names),
    Table(
        Project,
        rows=2_000_000,
        company=FanOut(Zipf(1.2)),
        created_at=Sequential(start, timedelta(minutes=1)),
        status=PerParent("company", last="ACTIVE", rest="COMPLETE"),
    ),
    invariants=[
        Invariant(
            "no company has two active projects",
            sql="""
                SELECT company_id FROM app_project WHERE status = 'ACTIVE'
                GROUP BY company_id HAVING count(*) > 1
            """,
        ),
    ],
)

build(shape)
```

## It is not a nuisance to work around

With fifty thousand companies and two million projects, `status='ACTIVE'`
matches **exactly fifty thousand rows: 2.5%** -- and that number is *derived
from the fan-out*, not chosen. So `status` skew and fan-out are the same
declaration seen twice.

Declare `Skew({"ACTIVE": 0.1, ...})` beside that fan-out and you have asked for
two hundred thousand active projects in a schema that permits fifty thousand.
The data will not load; and if somebody dropped the constraint to make it load,
it would carry the wrong selectivity for the one query the schema exists to
serve.

It is the same rule this package states everywhere else -- **a distribution is
declared over a fixed count, never as a multiplier** -- except that here one
distribution is *derived from* another rather than declared beside it.

## Three nets, decreasing coverage and increasing certainty

| Net | What it covers | When it fires |
| --- | --- | --- |
| 1. Generate it right | one rule, exactly | never fails: the rows come out satisfying it |
| 2. Check it | any rule you can write as a query | after the load, failing the build |
| 3. The database refuses | only what the schema states | during the load |

Net 3 is free -- rows go into the real migrated schema -- and it has a terrible
error message. So the arithmetic is also done statically, at declaration time,
before a row is generated: see [the pre-check](#the-pre-check) below.

## `PerParent` -- generating it right

```python
status = PerParent("company", last="ACTIVE", rest="COMPLETE")
```

One primitive covers most of the class: one active project, one default
address, one current subscription period, one primary contact, N winners per
contest.

- `last=` puts the value on the final rows of every group, `first=` on the
  opening ones. Exactly one of the two.
- `rest=` is what every other row of the group gets. A plain value, or a
  `Skew` or `Constant` -- a distribution that can say which values it produces,
  and therefore prove it does not produce the special one.
- `count=` is how many rows of each group are special. It is what
  N-winners-per-contest is for; a unique constraint is the case it is not.
- A group smaller than `count` has every row special. That is arithmetic, not a
  clamp: two entries cannot produce three winners.
- A **childless** parent contributes nothing, which is why the achieved count is
  one per *non-empty* group.

### Assignment order is not emission order

To say "the last project is `ACTIVE`" you have to know a group. To keep
[physical placement](relations.md) honest you have to emit a parent's children
interleaved. Those conflict for any design that has to *see* a group.

They do not conflict here, because a `FanOut` is a **partition of the child key
range** rather than a draw per child. Parent `j` owns child slots
`[start_j, end_j)`, so a row's position within its group and the size of that
group are both O(1) arithmetic on the row index and the seed. Nothing is
buffered, nothing is sorted, and the rows still stream one at a time into
`COPY`.

That is the fifth time this package has met the same split between one order and
another -- placement, derivations, fan-out partitioning, projection ordering,
and now this.

### `order_by` is a claim that is checked, not a sort

```python
status = PerParent("company", order_by="created_at", last="ACTIVE", rest="COMPLETE")
```

`order_by` claims that the last row of a group *under that column's ordering* is
the last row of the group *as the fan-out partitioned it*. Two conditions make
the claim true, and both are checked when the table is declared:

- the column climbs with the row index -- it implements `Ascending` and says so,
  which `Sequential` does when its step is positive; and
- the fan-out is `placement="grouped"`.

**`order_by` and `placement="arrival"` are mutually exclusive, and that is a
meaning rather than a missing feature.** Arrival interleaves a parent's children
through the table on purpose; their row indices are then scattered, so the last
row of a group is an ordinary one of them rather than the newest. The refusal
names both ways out.

Dropping `order_by` costs nothing the planner can see. PostgreSQL keeps no
statistic about which row of a group holds which value, so the selectivity, the
plan and the cost are identical either way. What `order_by` buys is that the
active project is the *newest* one, which is realism for the application.

## The pre-check

The models' own constraints are read from `Model._meta.constraints` when the
`Shape` is declared, and a contradiction is refused with the arithmetic in it:

```
one_active_project_per_company permits at most 50000 rows with status='ACTIVE',
one per (company); Project.status is filled by Skew({'ACTIVE': 0.1, ...}), which
asks for 200000 of them. A rule about a group cannot be kept by a draw made per
row, at any weight [...]
```

It runs on the **shape** rather than on the table, because that is where both
numbers live: a table knows how many rows it declares, and only a shape knows
how many companies there are.

!!! warning "`_meta.total_unique_constraints` is the helper that sounds right"

    It deliberately excludes conditional constraints -- it exists to answer
    whether a relation is one-to-one, and a constraint that only sometimes
    applies cannot answer that. So the one helper whose name fits skips exactly
    this case. `_meta.constraints` is read directly.

### What it decides

- **An unconditional `UniqueConstraint` is pigeonhole**, and provable: two
  million rows needing distinct `(company, label)` pairs cannot be built from
  fifty thousand companies and three labels, whatever the seed. This is the
  multi-column analysis a single `Table` declines to attempt.
- **Enough room is not a way to fill it.** An unconditional constraint over
  **two fan-outs** — the through table of a many-to-many — passes the pigeonhole
  comfortably, because the product of two parent counts dwarfs the row count,
  and still cannot be built. A fan-out is a partition of this table's rows over
  one parent's keys, computed from the row index alone; two of them partition
  the same rows without either seeing the other, so which pairs come out
  together is an artefact of that index and a collision is a matter of the seed.
  It used to be accepted and then die inside `COPY` at a row number that moved
  when the seed did. A deduplicated edge table is filled by a statement instead
  — `Projection(Membership, columns=(...), sql=...)` — which the refusal says.
  **One** of the two fan-outs having no group of two is enough to exempt it, by
  the same proof as below: such a fan-out never repeats a parent key, so no two
  rows share that column and the pair is distinct on that half alone.
- **The second column does not have to be a partition for that to be true.**
  One fan-out beside a drawn column is refused on the same proof:
  `Seat(company=FanOut(Zipf()), label=Skew({"a": 1, "b": 1}))` over fifty
  companies fits a hundred rows into exactly a hundred pairs, and a company that
  ends up with three seats still has only two labels to draw from. A
  `Distribution` is a pure function of the row index and of a draw taken from
  the field name and that same index, so it can no more see the fan-out's
  assignment than a second fan-out could, and nothing enumerates the
  combinations *inside a group*. The remedy is a value derived from the group
  rather than drawn beside it — `Derived("company", compute=..., scope="group")`
  receives this row's position among its parent's children and how many there
  are — and the refusal says so.
- **A partition with no group of two is exempt**, because a collision under
  such a constraint is always two rows of the *same* group drawing the same
  value. Flat sizes with no `childless` share give every parent `rows / parents`
  children, so at `rows <= parents` every parent gets zero or one -- and the
  parent has to be declared in the same shape, because a bound resting on a row
  count this package cannot read is not a bound. One row past that, some parent
  gets two and the refusal comes back.

    !!! warning "That exemption is the shape this package argues against"

        A fan-out giving every parent exactly one child is the uniform fan-out
        that makes the planner always right -- the one database in which join
        misestimation cannot occur. Prefer a `Zipf` and a constraint you can
        actually keep. But it builds, and a refusal must never tell you a shape
        cannot be built when it demonstrably can.

- **A column that is distinct in every row is exempt**, because a pair is
  distinct as soon as either half is and there is then nothing to arrange.
  `Sequential` says so about itself through
  [`Distinct`](reference.md#django_data_shape.distributions.distinct.Distinct),
  so `(company, invoice_number)` builds. A distribution that does not implement
  the protocol is read as not distinct, which is the safe direction: that costs
  a refusal answered by adding one method, where the other reading costs a load
  that fails at a row number which moves when the seed does.
- **A conditional `UniqueConstraint` is categorical.** A per-row draw cannot
  keep a per-group rule *at any weight* -- 2.5% is as broken as 10%, just later
  in the load -- so the refusal does not depend on the arithmetic, only the
  message does.
- A `PerParent` over one of the constraint's own fields, with `count=1` and the
  constrained value, is accepted. Grouped by something else, or with `count`
  above one, or with a `rest` that writes the value too, it is refused by name.
- A declaration that provably never writes the value -- a `Constant` of
  something else, a `Skew` that does not list it -- is accepted.

### What it cannot decide, and leaves to the other two nets

- A condition that is not a single equality: `Q(status__in=[...])`, a negation,
  two clauses joined. Reading one would mean this package deciding what an
  arbitrary predicate matches.
- A constraint written over `expressions` rather than `fields`.
- A conditional constraint whose grouping columns include no declared `FanOut`,
  because there is no partition here to satisfy it with and a refusal would name
  no remedy.
- A column filled by a distribution that cannot enumerate itself, and a fan-out
  over a parent this shape does not build. Both make the *capacity* unknown, so
  a conditional refusal drops its arithmetic and an unconditional one falls back
  to the structural sentence — neither stops the refusal itself.
- A fan-out with a `null` share, for the capacity in the same way: PostgreSQL
  counts each NULL in a unique index as distinct, so those rows are exempt from
  the constraint and a number computed as though they were not would not be true
  of this shape. The rows that do have a parent are unarranged exactly as
  before.
- A column filled by a `Derivation` under an unconditional constraint. A
  derivation reads something other than its own row index, so it is the one kind
  of declaration that *can* be arranged around a group — and whether a
  particular `compute=` is arranged around it is not readable from here.

In every one of those the declaration is allowed through, and net 2 and net 3
are what catch it.

Whether to refuse is decidable without the parent's row count; only the
numbers in the message need it. A project that builds its fifty companies with
the ORM and asks this package only for the projects still gets the refusal, and
gets it phrased per group rather than in rows.

## Declaring an invariant

Net 2 is the only one that covers rules the database does not enforce, which is
most of them: a denormalised total, a tenant id that must match its parent's, an
interval chain with no gaps, a status history whose transitions are legal.

Two ways to write one, and they are mutually exclusive:

```python
Invariant(
    "no project predates its company",
    Project,
    violated_by=Q(created_at__lt=F("company__founded_at")),
)

Invariant(
    "debits equal credits",
    sql="""
    SELECT transaction_id FROM app_entry
    GROUP BY transaction_id HAVING sum(amount) <> 0
""",
)
```

- `violated_by` is a `Q` describing the rows that are **wrong**, not the rows
  that are right -- because the wrong rows are what a failure has to report. It
  runs through `_base_manager`, so a default manager that filters cannot hide
  the rows the rule exists to catch.
- `sql` is a full statement rather than a predicate, because the interesting
  rules are aggregates. **Every row it returns is a violation**, and a statement
  returning nothing passes. It may read any table, including ones this shape
  does not build.

### A violation is a build failure

The checks run at the end of `build()`, **inside the transaction that loaded the
rows**, so a violation rolls the whole build back and the database is left as it
was found. An invariant that failed the *test* instead would leave a database
full of impossible data for every later assertion to be evaluated against, and
those assertions would pass or fail for reasons unrelated to the code.

The message names the rule, quotes the rows that broke it, and is written to be
read out of a terminal rather than stepped through in a debugger. The first
failure stops the run: a generator that broke one rule has usually broken the
ones downstream of it, and a report listing five consequences of one cause hides
the cause.

`check_invariants` is exported as well as called, so the same rules can be run
against a database this package did not just build -- a template clone, a
restored dump, or the state a suite has worked itself into.

!!! note "An invariant changes no row and still changes the cache key"

    The check runs during the build, and a cache hit is exactly what skips the
    build -- so a rule that made no difference to `shape_digest` would be a rule
    that silently stopped running the second time. That is worse than no rule,
    because it is a rule everybody believes. The cost is one rebuild of a
    database that would have been byte-identical.

## Where this stops

**A constraint must be satisfiable by construction within one group, or it is
declared as an invariant and checked, not generated.**

"No two shifts overlap **and** every shift is covered **and** nobody exceeds
forty hours" is scheduling. It is NP-hard, it needs a global search, and a
global search cannot stream into `COPY`. This package will not attempt it. Write
the rule as an `Invariant` and find out, or build that table with something whose
job it is.
