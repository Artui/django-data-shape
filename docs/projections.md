# Projections

Some tables are not distributed at all. They are **copied**.

An `Event` is created from a `Template`, and its `EventSession` rows mirror that
template's `TemplateSession` rows one for one. How many sessions an event has is
not a shape anybody chose: it is `count(TemplateSession WHERE template =
event.template)`. A `Projection` declares that.

```python
from django_data_shape import Constant, FanOut, Projection, Shape, Table, Uniform, Zipf, build

build(
    Shape(
        Table(Template, rows=500, name=Constant("t")),
        Table(
            TemplateSession,
            rows=4_000,
            template=FanOut(Zipf(1.1)),
            title=Constant("session"),
            minutes=Uniform(15, 90),
        ),
        Table(Event, rows=200_000, template=FanOut(Zipf()), name=Constant("e")),
        Projection(EventSession, per=Event, copying=TemplateSession),
    )
)
```

That reads as *one `EventSession` per `Event`, copying `TemplateSession`*, and it
is the whole declaration. The join, the column list and the primary keys are all
derived from the model graph, and the table is filled by a single
`INSERT ... SELECT` over the tables already built.

## Why a projection rather than a way of saying "mirror this"

Three reasons, and the first is the one that decides it.

**It is what the real system already collapses into at scale.** One event built
from a template is a service call. A million of them is one statement. A
projection *is* that statement, so a test database is built the way the
production table would be backfilled rather than the way one row is created — and
that is also why this is not the per-row creation hook this package
[declines](derivations.md).

**It needs no new distribution machinery.** A mirroring mode on `FanOut` would
need an inverted fan-out, a cardinality derived from another table, and a way to
say "as many as over there" — three new vocabulary items to express something
that is not a distribution in the first place.

**It reproduces a correlation Postgres cannot see.** Sessions-per-event is
correlated with the template, so every event built from a big template has many
sessions and every event from a small one has few. A plain
`FanOut(Zipf())` on `EventSession.event` would draw that count independently and
hand the planner a join selectivity real data never has. Postgres has no
cross-table statistics, so it cannot see the correlation either way — which means
declaring it does not fix an estimate. It builds the database in which the wrong
estimate is reproducible.

## The cardinality is determined, not declared

There is no `rows=` on a `Projection`. The row count is decided by the tables it
copies from, and declaring it as well would be the same over-determination this
package refuses everywhere else.

The achieved count comes back in the `BuildResult`, like every other table's:

```python
result = build(shape)
sessions = next(table for table in result.tables if table.table == "app_eventsession")
print(sessions.rows)
```

It also means a projection needs no scale factor of its own. `scaled_shape`
passes it through untouched, and it grows because the tables it reads did.

### It grows as a product, which is the part that surprises

A projection is one row per *pair* along the join, so its size is the inner
product of the two sides. When both of them fan out over the same parents, the
busy parents multiply:

```python
Table(Venue, rows=40, name=Constant("alhambra"))
Table(VenueSeat, rows=24_000, venue=FanOut(Zipf(1.1)), number=Sequential(1, 1))
Table(Show, rows=4_000, venue=FanOut(Zipf(1.15)), title=Constant("an evening with"))
Projection(ShowSeat, per=Show, copying=VenueSeat)
```

```
boxoffice_order      300,000     <- the largest number in the declaration
boxoffice_showseat 2,413,223     <- eight times bigger, declared nowhere
```

Raise either declared count by four and the projection grows by **sixteen**.
That is the model working as intended -- it is what reproduces a correlation a
`FanOut` on the child would destroy -- but it does mean the largest table in a
database can be the one nobody wrote a number for.

### `max_rows` bounds it, before the insert rather than after

```python
Projection(ShowSeat, per=Show, copying=VenueSeat, max_rows=3_000_000)
```

The count is taken first and compared, so a declaration that has run away costs
a scan of the join rather than the time to write every row of it. The refusal
names the number it would have written and the tables the join is over, because
the surprise is never the ceiling:

```
The projection into boxoffice_showseat would insert 12,058,114 rows, which is
over its declared max_rows=3,000,000. Nothing was written. A projection has no
row count of its own: its size is one row per pair along the join it copies,
over Show, VenueSeat. That size is a product, so when both sides of the join fan
out over the same parents the busy parents multiply and raising either declared
count by four grows this by sixteen. Either the ceiling is too low, or one of
those counts moved further than it looked.
```

`scaled_shape` multiplies the ceiling by the factor, for the same reason the
size needs no factor at all: every table scales, parents included, so a parent
has the same number of children at every factor and the projection is a sum over
`factor` times as many parents of an unchanged per-parent product. A ceiling
that stayed put would fire on the first growth assertion.

A declaration that does not set one is not charged for the answer -- no count is
taken. **There is no default ceiling and there will not be one**: how many rows
is too many is a judgement about size, which this package does not make on your
behalf anywhere else either. What it can do is act on your number, early.

## What gets derived

**The join.** `per` and `copying` are joined through a model they both reach in
one step — here both have a foreign key to `Template`. `per`'s own primary key
counts as a step of length zero, so a source that points straight at `per` is the
same case. Exactly one such link has to exist; zero and several are both refused
by name.

### `through=` — which model the join runs on

Several links is the common case, not the exception, and the reason is a pattern
most Django schemas have:

```python
class BaseModel(models.Model):
    created_by = models.ForeignKey(User, ...)
    updated_by = models.ForeignKey(User, ...)

    class Meta:
        abstract = True
```

An abstract base like that makes **any two models in the schema joinable**, so
the derivation has candidates everywhere and can settle nothing — not for one
pair, for every pair. `through=` says which model the join is on:

```python
Projection(RunStage, per=PlanRun, copying=PlanStage, through=Plan)
```

The refusal names it, and names a model that can actually resolve the join
rather than the first one alphabetically — an audit model is reached by two
edges from each side, so it is never a usable answer even though it is a real
candidate.

Where a model is reached by more than one edge from a side, `through=` cannot
help: both edges satisfy it, so naming it leaves the same choice. The refusal
says so and sends you to `sql=`, which is the only place a specific pair of
columns can be written.

**The columns**, in this order, for every column but the primary key:

| The column | Gets |
| --- | --- |
| the foreign key to `per` | that row's key |
| a foreign key to `copying` | the copied row's key |
| a column named the same as one on `copying` | that column's value |
| a column with a plain `default=` | that default, as a bound parameter |
| a nullable column, or one with `db_default` | nothing — it is left out |
| anything else | a refusal naming the column |

The copy is by name, so the two columns have to hold compatible types; the
database says so if they do not.

A `default=` is written rather than left out because a Django default is applied
by `save()` and is not DDL — the same reason `Table` fills one in. A **callable**
default is refused, and a projection has a second reason on top of `Table`'s:
the rows are made by one statement and never pass through Python, so there is no
per-row moment at which the callable could be called.

## Where the rows land, and why there is no `placement=`

The statement orders by `per`'s key and then the copied row's. That is
deterministic — two builds of one shape have to agree — and it is also the honest
physical layout.

`FanOut` has to choose between `arrival` and `grouped` because a parent's
children really do arrive over time, so emitting them contiguously is a lie. A
copied collection is different: one event's sessions are written in one
transaction, and the events themselves arrive in key order. Grouped **is**
arrival order here, so there is nothing to choose.

## Keys

A projected table's keys come from the same place as any other table's: the key
strategy on the declaration. The only extra requirement is that the strategy can
say itself in SQL, because a projection has no declared row count to enumerate in
Python and the rows never pass through it.

`SequentialKeys` — the strategy inferred for an integer primary key — can:
`row + 1`, over a row index the database computes with `row_number()`. `UuidKeys`
and `KeyFunction` cannot, and are refused by name at declaration time. That is a
refusal rather than an approximation on purpose: computing a different hash in
SQL from the one Python computes would give one strategy two meanings depending
on which statement filled the table.

A UUID-keyed table can still be projected — with `sql=`, producing the keys in
the statement.

## `values=` — a column of the projected table's own

A projection copies a column from the source it names, or takes the model's own
default. Those are its only two answers, and a projected table's **measure**
column is neither — the score on a review, the amount on a generated line, the
reading on a sample. It belongs to the projected row and to nothing the source
carries.

Leaving it to a model default is legal and is the wrong answer *here
specifically*: one value across every projected row is `n_distinct = 1`, the
exact shape a planner cannot use. A package whose whole purpose is planner
realism would be building a table it had made unplannable, and the declaration
would look correct.

```python
Projection(
    ReviewScore,
    per=Review,
    copying=Criterion,
    values={"score": SqlValue("({per}.id * 31 + {source}.id * 17) % 5 + 1")},
)
```

`{per}` and `{source}` are substituted with the aliases the derived statement
uses, quoted for the connection. They are placeholders rather than the aliases
themselves because the aliases are this package's private business: a
declaration that spelled them would break the day they changed, and a reader
could not tell which side was which. A literal `%` is escaped for the same
reason -- the statement is executed with bound parameters, so an unescaped one
is an incomplete placeholder to psycopg and to Django's SQLite wrapper alike,
and the paramstyle is no more the declaration's business than the aliases are.

### The expression is the one part of a shape that is not portable

Everything else here is a declaration compiled per backend. An expression is SQL
the database evaluates as written, and nothing can inspect it: `mod(x, 5)`
returns an integer on PostgreSQL and a REAL on SQLite, so one declaration writes
`5` into one database and `5.0` into the other. That is why the example above
spells modulo as `%`, which is integer on both. Write the expression for the
backend the shape is built on, and cast where it has to be both.

The escape hatch below answers the same question and answers it expensively —
`sql=` replaces the whole `SELECT`, so the join stops being derived from the
model graph, the copied columns are written out by hand, and the key strategy
has to be spelled in SQL. `values=` gives up none of that.

### Why SQL and not a distribution

A `Distribution` computes from `draw(stream, row)`, which is SplitMix64 —
expressible in PostgreSQL only through `numeric` modular arithmetic and casts
across the sign boundary, where one mistake gives a declaration two meanings
depending on which statement filled the table. That is exactly the divergence
[`SqlKeys`](keys.md) exists to refuse, and it is not worth buying convenience
with. An expression you wrote is honest about being yours.

## The escape hatch

For anything shaped oddly — a filter, an aggregate, a three-way join, a window —
supply the `SELECT` yourself and name the columns it produces.

```python
Projection(
    EventSession,
    columns=("id", "event", "title", "minutes", "channel"),
    sql=(
        "SELECT row_number() OVER (ORDER BY e.id, t.id), e.id, t.title, t.minutes, %s "
        "FROM app_event e "
        "JOIN app_templatesession t ON t.template_id = e.template_id "
        "WHERE t.title <> %s"
    ),
    params=("web", "hidden"),
)
```

The columns are **field names**, checked against the model, so a typo is refused
here rather than by the database. A relation may be spelled either way —
`event` and `event_id` name the same column — because what an `INSERT` lists is
the second, and that is what the refusal asking for `columns=` describes. The primary key has to be among them: this
package owns the keys, and a statement it did not write has to say what they are
rather than leave them to a sequence whose current value is not part of any
declaration.

Nothing else about the select is inspected — that is what an escape hatch is —
but the build still gives it the emptiness check, the sequence reset, the
`ANALYZE` and the transaction.

### `reads=` — what the statement selects from

Nothing here parses SQL, so a statement of your own is opaque and is ordered as
late as the rest of the declaration allows. That is the right default until
something fans out over the table it fills: the projection then has to run
*before* that table, and may find the tables it selects from still empty.
`reads=` says what it needs, and puts it back in the graph precisely — after
what it reads, before what reads it.

```python
Projection(
    EventSession,
    columns=("id", "event", "title", "minutes", "channel"),
    sql="SELECT row_number() OVER (ORDER BY e.id), e.id, e.name, 1, %s FROM app_event e",
    params=("web",),
    reads=(Event,),
)
```

It is part of the declaration the template-database cache keys on, because the
same statement run before and after a table selects different rows. A derived
projection has no use for it and is refused for asking: `per=` and `copying=`
already are the answer.

## Load order

A projection is ordered like everything else: after the tables it reads, before
anything that reads it. So the order the declarations are written in does not
matter, and **a projected table may itself be a fan-out parent**.

```python
Shape(
    Table(Attendance, rows=1_000_000, session=FanOut(Zipf()), name=Constant("a")),
    Projection(EventSession, per=Event, copying=TemplateSession),
    ...,
)
```

A raw `sql=` projection without `reads=` is the exception: it names nothing, so
"run it last" is a **preference** rather than an edge. It goes after everything
where nothing needs it, and directly before whatever does.

That distinction is the fix for a refusal that was simply wrong. Expressed as
edges to every other declaration, the preference met a table fanning out over
the projection and reported `Attendance -> EventSession -> Attendance` — a cycle
in a chain, with no way for the caller to correct it. A preference cannot
contradict a declared edge; an edge can, and did.

Two declarations that really do read each other are still refused by name, the
same way two tables fanning out over each other are — and the refusal happens
when the `Shape` is built, because which table can be filled first is decided by
the declarations and nothing else.

## Refusals

A projection that inserts **no rows** fails the build. An empty projected table
is not a smaller world — it is a declared table left out of the database, and
every test reading it then passes or fails for a reason unrelated to the code.
The usual causes are an input that is empty, and a join that matches nothing.

The rest are refused at declaration time, before a connection is opened: an
ambiguous join, a model with no edge to `per` or more than one, a column nothing
can fill, a callable default, and a key strategy with no SQL form.

## What is still Postgres-only

Nothing about a projection, and everything about what it is *for*. The statement
is ordinary SQL and runs anywhere, so `build(..., require_statistics=False)`
fills a projected table on SQLite exactly as it fills any other. What SQLite does
not get is the `ANALYZE` — and therefore a plan worth reading. The line is the
same one this package draws everywhere: generation and cardinality are
backend-neutral, planner realism is not.
