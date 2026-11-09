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

The models' own constraints are read from `Model._meta.constraints` and
`Model._meta.unique_together` when the `Shape` is declared, and a contradiction
is refused with the arithmetic in it:

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
  `Meta.unique_together` builds the same unique index and is checked the same
  way under every rule below; it has no name of its own, so a refusal names the
  tuple instead -- `unique_together ('company', 'person') needs ...`.
- **Enough room is never a way to fill it, and that holds with no fan-out
  anywhere.** Every mechanism this package has for filling a column computes it
  from the row index alone, so nothing enumerates a tuple and nothing keeps a
  single unique column distinct. Measured: fifty rows over three hundred
  combinations loaded **0 times in 20**; the same fifty rows over **ten
  thousand** combinations -- two hundred times the room -- still failed two. A
  single `unique=True` column drawn from fifty values for five rows loaded 17,
  and from a hundred values for ten rows, 10. Room moves the odds and never
  settles them, because a draw is not a permutation. All of these are refused at
  declaration time, and the exemption in every case is `Distinct`.
- **A `FanOut` on a `unique=True` column is refused**, because a partition
  exists to give some parent more than one row and a one-to-one permits none.
  It never loads -- 0 in 20 -- and it used to fall between the two checks:
  `unique=True` writes no entry in `_meta.constraints`, and a single table
  cannot see how many parents there are. The exemption is the same proof the
  others carry: flat sizes, no `childless` share, and rows no more than parents.
  `UniqueConstraint(fields=["company"])` and `unique_together = [("company",)]`
  write the same index and are decided the same way.
- **A column the shape leaves out is read as what the load leaves in it.** `COPY`
  does not name the column, so the database fills it. A literal `db_default` is
  one value in every row: the pigeonhole counts it as one, and beside a tree or a
  `FanOut(Zipf())` it loaded 0 times in 20 and is refused. A nullable column with
  none holds NULL, and each NULL in a unique index is its own group, so every row
  is out of the index and the constraint holds whatever the other columns do. A
  condition reads the same value: `Q(status=None)` over a column left out puts
  every row inside it, 0 in 20, and `Q(status="on")` over a `db_default` of `"off"`
  puts none.
  A `unique=True` column left out is read the same way: a literal `db_default` or
  `Now()` there is refused at two rows or more, since it loaded 0 times in 20.
- **`Now()` as a `db_default` is one value in every row too.** Django compiles it to
  `STATEMENT_TIMESTAMP()` on PostgreSQL and a table's rows arrive in one `COPY`, so
  every row holds one instant. Beside a skewed `Tree` or `FanOut` it loaded 0 times
  in 20 and is refused by name, and beside a tree that gives no row two children or
  a cap of one it loaded 20 times in 20 and is accepted. Every other expression is
  left to the other two nets (below).
- **A column may be named by its attname.** `fields=["parent_id", "name"]` is the
  index `fields=["parent", "name"]` is, and it is read as that one.
- **Under `nulls_distinct=False` a NULL is one value**, which takes away what
  rests on each being its own group. A column left out holds that one value in
  every row, and refusals read it so. A `FanOut` with a `null` share has one more
  group, as large as the share, so its cap of one proves nothing: `max_children=1`
  with `null=0.5` loaded 0 times in 20, and the pigeonhole counts the NULL as one
  more parent. The attribute arrived in Django 5.0 and is read with `getattr`.
- **A `Tree` beside drawn columns is refused the same way**, as in names
  unique among siblings: a tree gives a row several children whenever its sizes
  say so, and a drawn value cannot see its siblings. A tree's families are not
  ranges of rows, so no derivation is decided per family. The exemptions are
  `Distinct` and a tree that provably gives no row two children (below).
- **A `Tree` alone on a unique self-key is refused unless it gives no row two
  children**, in all four spellings: `unique=True`, a `OneToOneField`, a
  `UniqueConstraint` and `unique_together`. Sizes of one value, such as
  `Constant(1)`, fill it, as `FanOut(max_children=1)` fills a unique key to another
  table, and the refusal of any other sizes names that tree. A column that refuses
  NULL keeps no tree at all. Under `nulls_distinct=False` two roots collide, so what
  fills it there is one root, with `roots` at most `1 / (rows + 1)`, and sizes of one value,
  or a `Projection` with `columns=` and `sql=`.
- **A `Tree` beside a `FanOut`, or two `Tree`s, under one constraint are refused**
  for the reason two fan-outs are: each column is computed from the row index
  and neither sees the other. Measured over twenty seeds on tables named
  `testapp_branch` and `testapp_edge`, a tree beside `FanOut(Zipf())` loaded 0
  times at 200 rows over 50 companies and 10 times at 50 rows over 1,000; two
  trees loaded 0 times at 200 rows. The draws are keyed on the names, so another
  table's 10 is another number. `Paired` is refused over a tree, so what keeps the
  constraint is a `Distinct` column, a fan-out that gives no parent two rows
  (`max_children=1` where the parents can place every row, or the proof flat sizes
  make), a tree that gives no row two children, leaving the tree out, or a `Projection`
  with `columns=` and `sql=`.
- **A `Tree` in a conditional `UniqueConstraint` is refused** when a row can land
  inside the condition, as in one accepted reply per parent. A per-row draw
  cannot keep a rule about a family at any weight: on a table named
  `testapp_reply`, a 10% share loaded 3 times in 20 at 200 rows and a 2.5% share
  14, and another name moves those too. `PerParent` is refused over a tree, so
  what keeps the constraint is a filling of the column that stays outside the
  condition, a `Distinct` column in the constraint, a tree that gives no row two
  children, or a `Projection`. A `Tree` beside a `FanOut` keeps the `PerParent`
  remedy over that fan-out, because one row per company is enough to keep
  `(parent, company)`.
- **A relation named by the condition is refused** when a row can land inside it, as
  in `UniqueConstraint(fields=["company"], condition=Q(owner=1))` with `owner` a
  `FanOut`, a `Tree` or a `Paired`. Which row holds a key comes from the row index, so
  it is the per-row draw again, and size only moves the odds: over twenty seeds that
  shape loaded 20 times at 200 rows over 300 people and once over 10. `PerParent` is
  refused on a relation, so the message offers what would build for that declaration:
  `parents=` naming only keys outside the condition, no null share, a cap of one where
  the parents can place every row, sizes of one value or one root on a tree, the column left out, a `Distinct` column in the
  constraint, or a `Projection`. Accepted, each at 20 loads in 20:
  - nothing can land inside: `parents=(2, 3)` under `Q(owner=1)`, or `Q(owner=None)`
    with no null share. A `Paired` never writes NULL, and a tree always does.
  - a NULL in a column of the constraint itself, while each NULL is its own group,
    because those rows are out of the index: `Q(owner=None)` over `(owner, label)`.
    Under `nulls_distinct=False` they are one group, which loaded 0 times in 20.
  - at most one row inside: a single key under `max_children=1` or flat sizes, a single
    key on a tree that gives no row two children, and `Q(prev=None)` on one root. A set
    is not a single key: `Q(owner__in=[1, 2])` under a cap of one loaded 10 in 20.
  - `Paired` beside its own relation. Its partners are distinct inside each group of
    that relation, so `(company, person)` under any condition holds, and so does
    `Q(person=1)` over `company`. Under `nulls_distinct=False` with a null share on
    the relation that proof is gone, and it loaded 13 in 20. Anywhere else a `Paired`
    is refused like a fan-out: 11 in 20 under another fan-out, and 11 for a set of
    three partners over its own relation. Narrowing it with `parents=` is accepted and
    not offered, since it builds only if the partners left cover the busiest group.
  - one key of the relation a `Paired` in the constraint takes its groups from.
    `Q(company=1)` over `(person)` is one group, whose partners are distinct, and it holds
    at four rows of every company over four people, there beside a drawn column or another
    `FanOut` too. At thirty people it holds with a null share and under
    `nulls_distinct=False`, since a key holds no NULL. Two keys are two groups (`Q(company__in=[1, 2, 3])` loaded 2 in 20), and the
    rows a null share leaves had no group at all (`Q(company=None)` loaded 0 in 20).
- **That refusal needs no partition in the constraint**, because nothing it offers does.
  Over twenty seeds, on tables named for each shape: `Q(owner=1)` over a drawn `label`
  loaded 0 times at 200 rows and 15 at 20, `Q(prev=1)` over one 11, `Q(person=1)` over one
  with `person` a `Paired` 12, and `Q(person=1)` over `(person)` itself once. What the
  message offers loaded 20 in 20 on each of those: the narrowed keys, the column left out,
  a `Distinct` column, a cap of one, no null share, sizes of one value or one root on a
  tree. Where the `Paired` is the whole constraint only the column left out and a
  `Projection` are offered, since no other column can be `Distinct`.
- **A tree that gives no row two children keeps all three of those constraints**,
  beside any number of fan-outs, and it is a proof rather than a probability. Sizes
  holding one value, such as
  `Constant(1)`, weigh every row the same, so the largest remainder gives the
  first rows one child each and room lasts until the last child is placed. At most
  one non-root row, by arithmetic on `roots`, has no sibling whatever the sizes
  weigh. Neither depends on the seed or the table's name, and a load test holds
  the first of them on a real table. Both stand on each root's NULL being its own
  group, so under `nulls_distinct=False`, where two roots collide, such a tree also
  needs one root, which is one NULL as distinct as every key. A flat tree of many
  roots loaded 0 times in 20 there, and so did one root over siblings.
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
  that fails at a row number which moves when the seed does. It holds whatever
  else is in the constraint: beside two fan-outs, and under a condition, where it
  leaves each group one row and the condition nothing to count. It holds under
  `nulls_distinct=False` too, since a column of different values holds one NULL at
  most, and `(company, person, code)` over two `null` shares builds.
- **A conditional `UniqueConstraint` is categorical.** A per-row draw cannot
  keep a per-group rule *at any weight* -- 2.5% is as broken as 10%, just later
  in the load -- so the refusal does not depend on the arithmetic, only the
  message does.
- **The condition may name a set as well as a single value, however it is
  spelled.** `Q(status="DRAFT")`, `Q(status__in=[...])` and an OR of equalities
  over one column -- `Q(status="DRAFT") | Q(status="IN_REVIEW")` -- are read the
  same way,
  because what decides the refusal is whether a row can land *inside* the
  condition and membership answers that for both. The set form is the commoner
  one in real schemas -- "one open review per project" is usually several
  statuses rather than one -- so reading only the equality left the likelier
  spelling unchecked. An ORM offers no reason to prefer `__in` to the OR-tree,
  so making the arithmetic depend on which one a caller happened to write would
  be the same gap with an extra step.
- A `PerParent` over one of the constraint's own fields, with `count=1` and a
  `rest` that stays **outside** the condition, is accepted. Grouped by something
  else, or with `count` above one, or with a `rest` that also lands inside, it
  is refused by name. The set form is what makes that last case easy to write by
  accident: `last="DRAFT", rest="IN_REVIEW"` puts every row of every group
  inside a three-status condition, and it is the declaration that answers the
  equality form.
- A declaration that provably never lands inside the condition -- a `Constant`
  of something else, a `Skew` listing none of the members -- is accepted.
- A fan-out in the constraint that gives no group two rows, by `max_children=1` or
  the proof flat sizes make, is accepted whatever fills the condition, because a
  group of one row has nothing to count. Each loaded 20 times in 20. A `null` share
  under `nulls_distinct=False` is one more group, so it takes the proof away, and
  that shape loaded 0 times in 20.

- **A condition's own spelling is read, not guessed at.** `Q(state__isnull=True)` is
  `Q(state=None)`: over a column left out it puts every row inside, and it loaded 0
  times in 20, as `Q(state=None)` did. `Q(label__isnull=False)` is met by any value, so
  over a drawn `label` it is refused (0 in 20) and over a column left out it is accepted
  (20 in 20). A `Value` is the value it wraps: `Q(state=Value("x"))` over a 10% share is
  refused (0 in 20) and `Value("z")` accepted (20 in 20). An `F()` or another expression
  as the value is a member that may be met in any row, never one that is not:
  `Q(state=F("label"))` over two draws and `Q(owner=F("other"))` under a cap of one are
  refused, and loaded 0 and 18 times in 20.
- **A condition over a constraint that nothing partitions is refused** when a row can
  land inside it: `UniqueConstraint(fields=["label"], condition=Q(state="x"))` with both
  drawn, or `fields=["owner"]` with `owner` a `Paired`. Every row draws without seeing
  another, so two rows inside sharing a value is the seed. Over twenty seeds, at 200
  rows: a 10% `state` loaded 0 times and a 1% one 10, a `Constant("x")` 0, `Q(label="a")`
  on the drawn column itself 0, a `PerParent("company", ...)` in `state`, which keeps a
  rule about another group, 0, and `Q(owner=1)` with `owner` left out over a
  `db_default` of 1 0. Over a `Paired` a 10% `state` loaded 0 and the `PerParent` 0.
  What the message offers, each 20 times in 20 on the refused table's sizes: a column of
  the constraint that is `Distinct`, where one is drawn rather than paired; the condition
  column left out, where it is nullable, since its NULL meets no equality; and a
  `Projection`. A `PerParent` that writes none of the members is accepted, as any filling
  outside the condition is, and loaded 20 in 20.
- **Under `nulls_distinct=False`, a remedy that keeps a null share is not offered.** The
  share's rows are one group there, so a `Paired` over that relation, a
  `Derived(..., scope="group")` beside it and a cap of one are each offered with "no null
  share", and a `Paired` whose relation keeps one is refused: beside two tied fan-outs it
  loaded 0 times in 20, and 20 with the share dropped. A cap of one over a fan-out with a
  share loaded 0 times beside a draw, and 20 without it.
- **A cap of one is offered only where it can place every row**, or where the parents
  are not declared in this shape. `max_children=1` over
  fewer parents than rows cannot be partitioned, and the build refuses it before a row is
  written, so a tree beside a fan-out of 200 rows over 50 companies is not offered one,
  and the cap declared there is refused at `Shape()`, naming the arithmetic. Over 300
  companies it is offered and accepted. A `OneToOneField` capped over fewer parents than
  rows is refused the same way.
- **A NULL declared in every row is read as one.** `Constant(None)`, which a Python
  `default=None` becomes, keeps every row out of the index while each NULL is its own
  group, so beside a fan-out and alone it is accepted (20 in 20 each), where the
  pigeonhole used to count it as one value. Under `nulls_distinct=False` it is that one
  value, and refused (0 in 20).
- **A `Paired` whose relation has one parent is distinct in every row.** Its partition
  is one group, and partners are distinct inside a group, so every row holds a partner
  no other row holds. Pinned by `parents=(1,)` or one company declared, it is accepted
  under any condition, `Q(owner=1)` and a drawn `Q(state="x")` alike, 20 times in 20.
- **A unique column left to one `db_default` is offered what builds for its type.**
  `Sequential(1, 1)` writes 1 to `rows`, which an integer, a float, JSON and a duration
  take, 20 times in 20 at 200 rows. A small integer, positive or not, takes it up to 32767
  rows and fails one row past them, out of range. Text takes it where `max_length` holds the digits of
  `rows`: three characters at 200 rows loaded 20 times and two none, too long. A decimal
  takes it where the digits before the point do: `(5, 2)` loaded 20 and `(4, 2)` none, an
  overflow. A time, an IP address, binary and a boolean take it never, and are offered a
  `Projection` alone. A date, a datetime and a UUID keep the offers they had.

### What it does not read, and leaves to the other two nets

- A condition that describes no set of values for one column: a negation, two
  clauses joined by AND, a comparison like `__gt` (written either as a keyword
  argument or as a bare lookup expression), an `__in` over a queryset, or an OR
  whose branches name **different columns**. Reading one would mean this package deciding what
  an arbitrary predicate matches, which is the database's job. A set the caller
  wrote out is not that -- it is a list of values, and deciding membership in it
  needs no planner.
- A constraint written over `expressions` rather than `fields`.
- A derivation in a conditional constraint's group, beside a relation in the condition
  or a drawn one, since whether its `compute=` is arranged is not readable here:
  `Q(owner=1)` over a `label` numbered per company loaded once in 20, because the
  numbers repeat across companies, and numbered per owner it loaded 20 times in 20.
- A column the shape leaves out whose `db_default` is an expression other than
  `Now()`. Nothing here can read what it writes: `gen_random_uuid()` is new in every
  row and loaded 20 times in 20, and a call this package has not read may do either.
- A column filled by a `Derivation` under an unconditional constraint. A
  derivation reads something other than its own row index, so it is the one kind
  of declaration that *can* be arranged around a group — and whether a
  particular `compute=` is arranged around it is not readable from here. A
  `Derivation` filling the condition column of a `Tree`'s conditional constraint
  is left alone for the same reason, except `PerParent`, whose values are
  readable.

In every one of those the declaration is allowed through, and net 2 and net 3
are what catch it.

One more is allowed through and caught before either: a cap of one alone whose null
share leaves room for the rows that have a parent, such as
`FanOut(Zipf(), max_children=1, null=0.8)` with 200 rows over 50 companies. The build
counts every row against the parents, the share included, and refuses it when the
partition is drawn, naming the arithmetic, so it never reaches `COPY`. That refusal is
left to the build.

### What it cannot count, and refuses anyway

Not knowing a number is not the same as not knowing the answer, so these stay
refused, and the arithmetic is what drops out of the message.

- A distribution that cannot enumerate itself, under a condition. Unknown is read
  as able to land inside it, so `Uniform(1, 9)` under `Q(status="accepted")` is
  refused although it never writes that value and loads 20 times in 20, over a
  `FanOut` as over a `Tree`. The remedy
  is a declaration that says what it writes, such as a `Skew`.
- A key under a condition, where `parents=` does not settle it. Nothing here reads a
  parent's keys before the build, so `Q(owner=99)` over five people and `Q(prev=500)`
  over two hundred rows are refused although each loads 20 times in 20, over a
  partition and over a drawn column alike. The remedy is `parents=`, or the column left
  out.
- A key over a drawn column that merely has room. `Q(owner=1)` over a `Uniform(1, 10**9)`
  loads 20 times in 20 and is refused, because a wide draw is not `Distinct`, and over
  three hundred people with a three-value `label` it loaded 19. Room moves the odds and
  settles nothing, as it does for a drawn tuple. The remedy is a `Distinct` column.
- An expression as a condition's value, which may be met in any row. `Q(state=F("label"))`
  over a `state` drawn from values `label` never holds, and `Q(owner=F("other"))` with
  `other` left out, so every row compares with NULL, each load 20 times in 20 and are
  refused. The remedy is a condition this package reads, or a `Projection`.
- A distribution that cannot enumerate itself and a fan-out over a parent this
  shape does not build, under an unconditional constraint. Both make the *capacity*
  unknown, so the refusal falls back to the structural sentence.
- A fan-out with a `null` share, while each NULL is its own group: those rows are
  exempt from the index, so a number computed as though they were not would not be
  true of this shape. The rows that do have a parent are unarranged exactly as
  before.

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
