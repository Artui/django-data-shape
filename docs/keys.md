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

## Why anything else is refused

Inventing values for a semantic column is not a small liberty. A character
primary key once loaded cleanly with the strings `"1"`, `"2"` and `"3"` -- values
the application could never have produced, with a whole statistics picture built
on top of them, and no error anywhere.

Refusing names the field and the type, and points at `keys=`.
