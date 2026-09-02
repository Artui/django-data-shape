# Relations

A foreign key is declared with a `FanOut`: how many children each parent has, as
a distribution rather than a number.

```python
from django_data_shape import Constant, FanOut, Shape, Table, Zipf, build

shape = Shape(
    Table(Company, rows=50, name=Constant("acme")),
    Table(
        Order,
        rows=2_000_000,
        company=FanOut(Zipf(1.2), childless=0.35),
        status=Skew({"complete": 0.98, "pending": 0.02}),
    ),
)

build(shape)
```

## Why a distribution and not a number

Giving every parent ten children is the tidy thing to do and it is the one shape
in which the query planner is never wrong.

Postgres estimates a join from `n_distinct` — the *average* number of children
per parent. When the real fan-out is uniform, that average is the truth, so the
estimate cannot miss. Give the head a thousand children and the tail one, and the
same estimate is wrong in both directions at once. Measured on a two-million-row
table with a Zipf fan-out, a join over the busiest parents was estimated at 3,880
rows against an actual 251,178 — a 65-fold under-estimate that picks a nested loop
over 193,000 heap blocks.

That defect cannot be reproduced against a fixture with even fan-out. Declaring
the distribution is what makes it reproducible.

## The parents do not have to be ours

`FanOut` reads the parent table's **real keys**. It does not assume the dense
`1..N` range this package assigns when it builds a table itself.

That matters because the realistic project is a hybrid: fifty companies created
through the ORM or `model_bakery`, where the row count is small and the ORM is
the right tool, and two million orders from here. Their keys are whatever the
sequence handed out, and a fan-out assuming `1..50` would point every child at
nothing.

```python
# The parents already exist, made however you liked.
companies = [Company.objects.create(name=f"c{i}") for i in range(50)]

# Only the large table is declared.
build(Shape(Table(Order, rows=2_000_000, company=FanOut(Zipf()), status=Constant("x"))))
```

Reading the keys is also what makes referential integrity hold **by
construction**: every key written came out of the parent table, so there is
nothing to validate afterwards.

## The childless tail

`childless=0.35` gives 35% of parents no children at all.

The share is a probability per parent, not a quota, so it converges rather than
holding exactly. Measured against a declared 30%: 15% at twenty parents, 27% at a
hundred, 30.2% from a thousand upward. **Asserting on the achieved share in a
small world is asserting on the seed** -- which matters for the scale harness,
where the small factors are deliberately tiny.

It is called out separately from the size distribution because it is the case
hand-written fixtures always omit, and the one that changes what a query returns:
a parent nobody references is the difference between an inner join and an outer
join giving the same answer and giving different ones.

## Nullable keys

`null=0.25` leaves a quarter of the children with a `NULL` foreign key. Only
meaningful on a nullable column, and worth declaring because `null_frac` is
planner-visible — an optional foreign key that is never null is its own kind of
unrealistic.

It thins the partition uniformly *after* it is computed, so the size distribution
describes the pre-null spread.

## Placement — where the children physically sit

`placement` decides the order children are written in, and it is not cosmetic.

| `placement` | `pg_stats.correlation` | What it is |
| --- | --- | --- |
| `arrival` *(default)* | below 0.3 | children interleaved, the way rows really arrive |
| `grouped` | above 0.9 | children contiguous per parent |

Same rows, same per-parent counts, different plan. On a clustered table Postgres
costs an index scan over the foreign key an order of magnitude cheaper, and picks
it where it would not otherwise.

The default is `arrival` because the obvious fixture loop —
`for c in companies: for _ in range(n): Order(company=c)` — produces the
*clustered* layout, which no production table has. A test database can be
unrealistic in the flattering direction, and that loop is.

## Load order

Tables load parents-first, whatever order they are declared in. Not because the
database insists — Django creates every PostgreSQL foreign key
`DEFERRABLE INITIALLY DEFERRED`, so the checks fire at commit and any order would
satisfy them — but because a fan-out reads the parent's keys, and a table with no
rows yet has none.

Two tables that fan out over each other are refused by name: one of them has to
load first, and a cycle means neither can.

## What is refused

- A relation declared with a **value distribution** rather than a `FanOut`. It
  would emit keys drawn from nothing, pointing at rows that may not exist.
- A `FanOut` on a column that is **not** a relation, which has nothing to fan out
  over.
- A **self-referential** `FanOut`. It would read keys from a table still empty at
  load time; self-referential trees are their own feature.
- A required relation **left undeclared**, which would fail the load on a
  not-null violation rather than at declaration time.
