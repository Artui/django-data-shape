# Derivations

A distribution says what a column looks like **across** rows. A derivation says
what one column is **given** the others.

```python
from django_data_shape import Derived, FanOut, Shape, Table, Zipf, build

shape = Shape(
    Table(Account, rows=50, signed_up_at=Sequential(start, timedelta(days=1)), plan=Skew(plans)),
    Table(
        Ticket,
        rows=2_000_000,
        account=FanOut(Zipf(1.2)),
        opened_at=After("account.signed_up_at", within=timedelta(days=365)),
        severity=Given("account.plan", {"free": low_skew, "enterprise": high_skew}),
        quantity=Aligned("size", Uniform(1, 100, places=0)),
        unit_price=Aligned("size", Uniform(1, 500, places=2)),
        total=Derived("quantity", "unit_price", compute=operator.mul),
    ),
)

build(shape)
```

## One mechanism, four faces

`Derived`, `After`, `Given` and `Aligned` are not four features. They are one
question -- *compute this from something already known* -- asked in three
different places, and the place is the only thing that varies:

| Face | Scope | Sources are | Example |
| --- | --- | --- | --- |
| `Derived` | `row` *(default)* | other columns of this row | `total = quantity * unit_price` |
| `After` | `parent` | `"relation.field"` on the parent row | an order opened after its account signed up |
| `Given` | `parent` | `"relation.field"` on the parent row | severity skewed by the account's plan |
| `Aligned` | `rank` | a shared rank the declaration names | the whales are the same whales |

`Derived` is the mechanism and the other three are shorthand over it. The scope
is a parameter, so a correlation nobody shipped a face for is still declarable:

```python
region = Derived("account.country", compute=region_of, scope="parent")
```

That is why they are one thing. Built separately, "custom creation logic" and
"correlate across a relation" become two vocabularies overlapping on the
interesting half -- and the half a consumer asks for first is the within-row one,
which is exactly what a `Derived` is.

## A derivation is not a distribution

The distinction decides what the package can do with each, so it is a type and
not a convention.

- A **distribution** answers *what is the marginal shape of this column across N
  rows*. That is the question the query planner asks. It is what decides whether
  an index is usable, and it is what a statistics target or a shape hash has to
  enumerate.
- A **derivation** answers *given this row's other values, what is this one*.
  Nothing about it is planner-visible on its own.

So `isinstance(x, Distribution)` and `isinstance(x, Derivation)` are never both
true, and nothing that enumerates the planner-facing half can pick one of these
up by accident.

## Column order is not computation order

`Table.columns()` sorts by name, because that order is the `COPY` column list and
two declarations differing only in keyword order have to produce the same
statement. It says nothing about what depends on what.

`total` depends on `unit_price`, and sorted by name `total` comes first. So
derivations get their own order -- a topological sort over row-scoped sources --
and a cycle among them is refused at declaration time, by name:

```text
Ticket cannot compute a, because its derivations depend on each other in a
cycle: a -> b -> a. One of them has to be computed first, and a cycle means
none of them can.
```

## Reaching the parent

A parent-scoped source is read out of the **parent table**, in the same query
that already reads the parent's keys for the fan-out. Two consequences worth
knowing:

- **The parent does not have to be ours.** Values are queried, not recomputed
  from a declaration, so fifty accounts made with `model_bakery` work exactly
  like fifty this package built. That is the same correction the fan-out took
  for keys.
- **They arrive as Python values, not as stored ones.** The read goes through
  the ORM so the field's own `from_db_value` runs -- a raw cursor hands back a
  naive datetime on SQLite where the application reads an aware one, and a
  `JSONField` as text rather than a dict.
- **The fan-out may not have a null share.** A child with no parent has no value
  to read, and substituting one would be an approximation. It is refused when the
  table is declared rather than met as a `None` inside the arithmetic.

Reaching the parent costs one query per relation per build, not one per row,
because a fan-out is a partition: which parent owns a child row is arithmetic.

## `After`

```python
opened_at = After("account.signed_up_at", within=timedelta(days=365))
```

The gap is spread uniformly over `within`, starting at `at_least` (zero by
default). It works in whatever unit the column uses -- `timedelta` for a date
column, a number for a numeric one.

Without it, two date columns joined across a relation have a selectivity no
production database has: every combination occurs, including the half that
cannot.

The result is **not** monotonic with the row, because the parents are not. A
column filled this way has a low `pg_stats.correlation` where `Sequential` gives
a high one. That is honest -- real children of scattered parents arrive
scattered -- but it is a different physical shape, and an index scan is costed
differently over it.

## `Given`

```python
severity = Given(
    "account.plan",
    {"free": Skew({"low": 0.9, "high": 0.1})},
    default=Skew({"low": 0.4, "high": 0.6}),
)
```

A parent value not listed and no `default` is refused **during the load**, naming
the column and the value. It is one of very few refusals here that cannot happen
at declaration time, and the reason is structural: the parent's values live in
the parent table, not in the declaration.

Worth being honest about what this buys. Postgres's own `CREATE STATISTICS`
cannot span tables, so the planner still estimates the pair as independent.
Declaring it does not fix an estimate; it builds the database in which the wrong
estimate is reproducible.

## `Aligned`

```python
storage_bytes = Aligned("size", Uniform(1e6, 1e12))
seat_count = Aligned("size", Zipf(1.1))
trial_days_left = Aligned("size", Uniform(0, 30), reverse=True)
```

Independent marginals give a database that is realistic per column and
unrealistic per **entity**: no single row is extreme in two ways at once, and
that row is the one that breaks production.

A rank is a name the declaration invents, and every column naming it reads the
same draw. The coupling is exact, and `reverse=True` is exact in the other
direction; there is no strength parameter, because a partial coupling is a
copula and a copula is a research project wearing a small API. A `Derived` over a
rank source can compute whatever it likes from the same draw.

Ranks are **per table** and **per row**. Two tables using the name `"size"` share
nothing, because the only thing they could align on is the row index, and row 40
of one table has no relationship to row 40 of another.

One thing an `Aligned` cannot do for you: a distribution that ignores its draw
aligns to nothing. `Sequential` is a function of the row index and `Constant` of
neither, so wrapping either one is accepted and does nothing.

## Your code may not call the database

This package may call your code. Your code may not call the database.

That is the boundary the whole package rests on, and unusually it is decidable
rather than advisory. Generation runs under a wrapper on the connection being
built, so a query raises `DerivationQueriedDatabase`, naming the table and its
derivations:

```python
def price(quantity, unit_price):
    return Rate.objects.get(pk=1).amount * quantity  # refused, at build time
```

The reason is not purity. `Model.objects.create()` per row is the thing this
package replaces; a hook whose body may query is a hook whose body will, and then
nothing is `COPY`-loaded and this is a slow fixtures library with extra
vocabulary. Read what you need before the build and close over it.

The check sees queries on the connection being built. Code that reaches a
different alias, or another thread's connection, is outside what a wrapper on one
connection can observe -- the rule still holds there, and only its enforcement
stops at the edge of the connection.

## What is refused

- A row source that is **not a declared column** of the same table.
- A parent source that does not name `relation.field`, names a relation that is
  not a declared `FanOut`, or names a field the parent model does not have.
- A parent source across a fan-out with a **null share**.
- A **cycle** among row-scoped derivations, named as a cycle.
- A derivation on a **relation column**, which needs a `FanOut`; it would emit
  keys drawn from nothing.
- A `Derived` with **no sources**, which is a `Constant` said with a callable, or
  one whose `compute` is not callable.
