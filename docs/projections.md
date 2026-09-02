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

## What gets derived

**The join.** `per` and `copying` are joined through a model they both reach in
one step — here both have a foreign key to `Template`. `per`'s own primary key
counts as a step of length zero, so a source that points straight at `per` is the
same case. Exactly one such link has to exist; zero and several are both refused
by name.

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
here rather than by the database. The primary key has to be among them: this
package owns the keys, and a statement it did not write has to say what they are
rather than leave them to a sequence whose current value is not part of any
declaration.

Nothing else about the select is inspected — that is what an escape hatch is —
but the build still gives it the emptiness check, the sequence reset, the
`ANALYZE` and the transaction.

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

A raw `sql=` projection is the exception: nothing here parses SQL, so it names
nothing it reads and is ordered after every table and every derived projection.

Two declarations that read each other are refused by name, the same way two
tables fanning out over each other are.

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
