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

The weights are **numbers**, and they are normalised, so their scale does not
matter: `Zipf()` for a realistic heavy tail, `Uniform(1, 10)` for something
flatter, `Uniform(1, 10, places=0)` if you would rather say a count is a whole
number. `int`, `float`, `Decimal` and `Fraction` all work.

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

## Narrowing which parents

`parents=` spreads the table over the keys it names and no others:

```python
Table(Session, rows=50, company=FanOut(Constant(1), parents=[tenant.pk]), label=Constant("s"))
```

Real keys, read out of the parent table like any other fan-out — a key that
matches no row is refused by name rather than quietly taking a smaller share.
A **list of keys, never a queryset**: a declaration needs no connection, and
evaluating a queryset here would give it one. Build the list yourself, which is
the query you were going to run anyway.

`childless=` is then a share of the parents *named*. A parent outside the list
has no children because it is not in this declaration, which is a different
statement from one weighed at zero.

### It is planner-visible, and pinning everything to one tenant is a lie

This is the argument against the obvious use. A column pinned to a single parent
has `n_distinct = 1`, and a planner that knows a tenant filter matches every row
will price it at nothing — so a tenant-scoped query looks free in the test
database and does not in production. That is the flattering, unreal shape this
library exists to remove, arrived at through the library.

Declare several tenants and make yours one of the heavy ones instead. The head
of the distribution is reachable through
[`fan_out_sizes`](#reading-the-fan-out-back), so a test can ask which parent got
the most rows rather than pinning to be sure:

```python
counts = fan_out_sizes(shape, Session, "company")
busiest, _ = counts.ranked()[0]
```

Pin when the shape of the tenant column is genuinely not what is under test —
a fixture for one screen, say — and know that you have given up any assertion
about how a tenant filter is planned.

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

## Many-to-many, and the second key of an edge table

Two fan-outs on one through table are refused, and the reason is the reason a
pairing exists: a fan-out is a partition computed from the row index alone, so
two of them partition the same rows without either seeing the other. Nothing
enumerates the pairs, and whether two rows collide is a matter of the seed.

`Paired` is the half that looks:

```python
Table(
    Membership,
    rows=50_000,
    company=FanOut(Zipf()),
    person=Paired("company", Zipf()),
    role=Constant("member"),
)
```

`company` partitions the rows as any fan-out does — that is the **declared**
degree distribution, and it is exact. Within each of its groups, `person` takes
that many **distinct** partners, so a duplicate pair is impossible by
construction rather than removed afterwards. The edge count is therefore exactly
`rows`: nothing is deduplicated, and the build never reports an achieved count
against a requested one.

### What binds is the busiest group, not the product

Every row of one group needs a different partner, so the constraint is
**`largest group ≤ partners`** — not `rows ≤ companies × people`, which is far
larger and would let impossible shapes through.

A heavy tail puts a large share of every edge on one group: `Zipf(1.2)` over
5,000 companies puts **21% of the edges on the top one**. So 50,000 edges over
20,000 people means one company needs 10,681 distinct people, and a declaration
that looks sparse against the product is still refused. It cannot be known until
the partition is resolved, so this is the one structural refusal that waits for
the build rather than firing at declaration time.

### The second side is derived, and approximate

Both marginals plus the edge count is over-determined — fixing all three is a
constraint satisfaction problem, and a CSP cannot stream into `COPY`. So the
edge count and one side's distribution are declared, and the other follows.

What follows *approximates* `weights` rather than reproducing it. Measured
against exact weighted sampling without replacement, over 200,000 edges:

| | exact sampling | what this builds |
| --- | --- | --- |
| busiest partner | 4,774 | 5,223 (1.09×) |
| 99th percentile | 37 | 48 |
| partners touched | 44,758 | 41,571 |

It concentrates somewhat more than exact sampling, consistently and boundedly.
The numbers are here rather than only in the code because a derived shape nobody
quotes is a shape nobody can check.

It is an approximation with **no tuning parameter**, which is the bar the
alternatives failed: choosing partners one at a time and rejecting duplicates
costs 245 probes per row on that world, and the usual fix — sampling the ones to
leave out when a group wants more than half — is ten times faster and *a
different sampling rule*, so a group one row over the halfway mark would get a
materially different membership from one row under. A cliff at an arbitrary size
that no declaration mentions is not something this package will build.

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
