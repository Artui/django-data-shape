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

## One-to-one

A `OneToOneField` is a fan-out whose partition never gives a parent two rows,
and the spelling is a flat distribution over at least as many parents as this
table has rows:

```python
# over a Table(User, rows=50) or more
Table(Profile, rows=50, user=FanOut(Constant(1)), headline=Constant("hi"))
```

`Constant(1)` rather than `Uniform(1, 1)`, which is refused: an inclusive range
of one value is a range that says nothing, and a distribution asked for
variation it cannot supply is more likely a typo than an intention.

Anything skewed is refused here, and the refusal is not conservative. A skew
exists to give some parent several children and a one-to-one permits none, so
`FanOut(Zipf())` on a unique column does not merely usually fail -- measured at
fifty rows over a hundred parents, it loaded **zero times in twenty**.
`childless=` still asks for parents with no row at all.

## Nullable keys

`null=0.25` leaves a quarter of the children with a `NULL` foreign key. Only
meaningful on a nullable column, and worth declaring because `null_frac` is
planner-visible — an optional foreign key that is never null is its own kind of
unrealistic.

It thins the partition uniformly *after* it is computed, so the size distribution
describes the pre-null spread.

## Reading the fan-out back

Declaring a skew is only half of it. The reason to declare one is to write an
assertion about the head or the tail, and that needs to know *which* parent is
the head — which, without help, means a `GROUP BY` over the child table before
the measurement has started.

`fan_out_sizes` answers it from the declaration instead:

```python
from django_data_shape import fan_out_sizes

counts = fan_out_sizes(shape, Order, "company")

whale, orders = counts.ranked()[0]  # the head
quiet = counts.ranked()[-5:]  # the tail
nobody = counts.childless()  # the parents with no children at all
assert counts[whale] == orders  # and it is an ordinary mapping
```

It costs one `SELECT` over the **parent** table. That is the asymmetry worth
having: fifty thousand parents against two million children, and an aggregate
would have to read the children.

This is what the partition representation is for. A fan-out is not "pick a
parent per child" but "parent `T` owns child rows `[start, end)`", and a
partition can be inverted where a draw cannot.

### It works on a cached build

`template_database` builds once and every later run *clones*, generating nothing
at all — so anything remembered from a build would simply not be there. Nothing
is remembered. The partition is a pure function of the declaration, the seed and
the parent's primary keys, so it is recomputed through the same code the build
runs. The clone holds the parents, the declaration holds the rest, and the
template cache keys on this package's version, so a database built by a release
that drew differently is never the one being read.

The one thing recomputation needs is that the parent table still holds the
parents the children were spread across. Where the parent is declared in the
same shape — which every cacheable shape is, since a template is built into a
freshly migrated database — that is checked, and a mismatch raises
`WorldChanged` rather than returning a plausible partition of a world nobody
built:

```text
Company holds 41 rows and this shape declares 40, so the fan-out for
Session.company would be spread over parents the children were never spread
across.
```

Where the parents came from the ORM instead, there is nothing to check against
and nothing is claimed: the answer describes the parents that are there. Ask
before the test starts making more.

### The sizes are not ordered on the parent key

**"The whales are the low ids" is false**, and so is the reverse. A parent's size
is drawn from a stream keyed on its position in the parent table, so the large
groups are scattered through the key range rather than gathered at either end.
`ranked()` is a real reordering of the keys, not the keys read forwards or
backwards.

That is deliberate and it is not going to change. Ordering the sizes on the key
would put a correlation between a parent's id and its number of children into
the child table — and a correlated foreign key is planner-visible, so it would
be this package manufacturing exactly the flattering, unreal shape it exists to
remove. Whichever end you reach for, reach for it through `ranked()` or
`childless()` rather than through `id=1`.

### With a null share the counts are the partition

`null=` thins the partition per row *after* it is computed, so under one of those
the counts are the partition and the rows pointing at each parent are fewer —
uniformly in expectation, not exactly. `counts.null_share` is the share it was
thinned by, and it is `0.0` whenever these numbers are row counts. The ranking is
unaffected either way, which is what the head and the tail are read for.

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
load first, and a cycle means neither can. That refusal happens when the `Shape`
is constructed rather than when the build starts, because which table can be
filled first is decided by the declarations and needs no connection to answer.

## What is refused

- A relation declared with a **value distribution** rather than a `FanOut`. It
  would emit keys drawn from nothing, pointing at rows that may not exist.
- A `FanOut` on a column that is **not** a relation, which has nothing to fan out
  over.
- A **self-referential** `FanOut`. It would read keys from a table still empty at
  load time; self-referential trees are their own feature.
- A required relation **left undeclared**, which would fail the load on a
  not-null violation rather than at declaration time. The message names the
  column and the fan-out that fills it: forgetting one foreign key is the
  commonest mistake there is, so it is the first thing many readers ever see
  this package say.
- A model using **multi-table inheritance**. One of its rows is two rows, in two
  tables, sharing a key — and this package fills one table per declaration and
  assigns that table's keys itself, so it can write either half and has nothing
  to pair them with. Declaring the two separately does not help: the child's
  primary key is a foreign key to the parent, and a fan-out is a partition
  rather than the bijection that would need to be. `_meta.concrete_fields` spans
  both tables while `db_table` names one, so this used to be accepted and then
  raise a bare `KeyError` from inside the loader. A proxy model is not this case
  and is fine: it adds no column and no table, so a shape naming one is a shape
  about the table it proxies.
