# Keys

This package assigns primary keys itself, using a **key strategy**: a
deterministic function from the row index to that row's key.

```python
from django_data_shape import Constant, KeyFunction, Table

# Inferred. An integer key counts from one.
Table(Order, rows=1_000_000, status=Constant("complete"))

# Inferred. A UUIDField key is derived from the seed.
Table(Tenant, rows=50_000, name=Constant("acme"))

# Declared, for a key type with no obvious answer.
Table(
    Page,
    rows=10_000,
    title=Constant("untitled"),
    keys=KeyFunction(lambda row: f"page-{row:06d}"),
)
```

## Why the keys are ours at all

Three things depend on it, and none of them depends on the keys being integers:

- **A foreign key is satisfied without a lookup**, because a child can compute
  its parent's key from the parent's row index.
- **A self-referential tree is acyclic by construction**, because
  `parent_index < child_index` holds on the *index*, not on the value.
- **Two builds of one shape agree**, because the same seed produces the same
  keys.

All three follow from determinism. Integers were only the most obvious
deterministic function, and treating them as the requirement is what once made a
UUID-keyed project unable to use this package at all.

## What is inferred

| Primary key | Strategy | Notes |
| --- | --- | --- |
| `AutoField`, `BigAutoField`, `IntegerField` | `SequentialKeys` | `row + 1`. The identity sequence is moved past the keys afterwards |
| `UUIDField` | `UuidKeys` | A version-4 UUID derived from the seed and the row. No sequence exists, so none is reset |
| anything else | *refused* | Declare one with `keys=` |

`UuidKeys` derives from a 128-bit hash rather than from a random draw. A random
key would make the primary key -- and every foreign key pointing at it -- differ
between two builds of the same shape.

### `Md5Keys`, for a UUID table a projection fills

A [projection](projections.md) has no declared row count, so its rows never pass
through Python and its keys are written by the statement that inserts them. That
needs the same rule on both sides, and `blake2b` has no PostgreSQL equivalent --
so `UuidKeys` cannot fill one, and used to be where a UUID-keyed projection
stopped.

`keys=Md5Keys()` is the same idea over `md5`, which exists in both places:

```python
Projection(Submission, per=Request, copying=Template, keys=Md5Keys())
```

**A different strategy, never a second meaning for `UuidKeys`.** The two draw
different keys for the same row, so one silently becoming the other where a SQL
twin was needed would change every key in every world already built. Declare it
where you need it.

The two halves are checked against each other rather than argued about: a test
computes fifty keys in Python and fifty in PostgreSQL and compares them.

`md5` here is **not** a security choice. Nothing authenticates anything, the
input is a table's own seed and row index, and md5's weakness is collision
resistance against someone who picks the input. Nobody picks these.

### Building beside rows your own code made

Both UUID strategies say they cannot collide with rows already in the table, so
building into one that already has some is allowed -- which is what makes
"parents from your factory, children from here" work. `SequentialKeys` assigns
from 1 and does collide, so that stays refused, and `KeyFunction` is read the
same way because this package cannot know what your function returns.

It is a claim about **keys and nothing else**. A unique constraint on another
column can still meet a row that was already there, and an invariant can still
be broken by rows this package did not write -- both are checked after the load,
against the table as it then stands.

## Declaring your own

`KeyFunction` takes any deterministic function of the row index:

```python
Table(
    Page,
    rows=10_000,
    title=Constant("untitled"),
    keys=KeyFunction(lambda row: f"page-{row:06d}"),
)
```

It is checked rather than trusted. At construction the function is called twice
over a sample and refused if it disagrees with itself, and refused again if it
produces a duplicate. A key that varies between calls would break reproducibility
silently, in the one column every foreign key points at.

## What `keys=` does not cover

`KeyFunction` takes any deterministic function of the row index, so there is no
key *type* left over -- if you can compute it, you can declare it.

One thing genuinely remains, and it is not a type: a **composite primary key** is
several columns, and a key strategy maps a row index to one value. That is arity
rather than type, so `keys=` cannot help and the declaration is refused by name.

## Why anything else is refused

Inventing values for a semantic column is not a small liberty. A character
primary key once loaded cleanly with the strings `"1"`, `"2"` and `"3"` -- values
the application could never have produced, with a whole statistics picture built
on top of them, and no error anywhere.

Refusing names the field and the type, and points at `keys=`.
