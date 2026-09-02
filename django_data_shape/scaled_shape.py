"""The same declaration at a different size."""

from __future__ import annotations

from django_data_shape.invalid_shape import InvalidShape
from django_data_shape.shape import Shape
from django_data_shape.table import Table


def scaled_shape(shape: Shape, factor: int) -> Shape:
    """``shape`` with every declared row count multiplied by ``factor``.

    This is the answer to the question a growth assertion asks -- *make the
    world be at factor F* -- and the choice worth stating is that a factor
    varies the **declaration**. The alternative, and the one that looks cheaper,
    is to build once at the largest factor and let a smaller factor see only
    part of it. Three things say otherwise:

    - **A subset is not a smaller database; it is the same database with a
      filter.** The table still holds every row, the statistics still describe
      every row, and an index still spans every row. Worse, the *block under
      test* would have to cooperate -- to restrict itself to the subset -- so
      the harness would leak into the code being measured. A growth assertion
      whose subject has to know it is being scaled is measuring the harness.
    - **A fan-out is a partition of the child key range, so taking a subset
      changes the shape rather than the size.** Cutting the children short
      removes whole parents under ``grouped`` placement and thins every parent
      under ``arrival``, so the childless share and the tail -- the two things
      the declaration exists to state -- would come out different at every
      factor, and the curve would be fitted over worlds of different shapes.
      Multiplying the row counts keeps the distribution and varies only its
      size, which is what "the same world, bigger" has to mean.
    - **A shape is inert, hashable data, and a scaled shape is another one.**
      That is the representation the template-database cache will key on, so
      each factor gets a cache key for free and caching makes a repeated factor
      cheap without changing this protocol. A subset has no key of its own; it
      is a query over somebody else's build.

    The cost objection does not survive the numbers either. Growth assertions
    run at small absolute scales -- a hundred rows against a thousand -- where
    building is milliseconds and where the question is the *shape of the count
    curve* rather than plan realism. The two-million-row build is the plan
    assertion's problem, and plan assertions do not vary a factor.

    Every table is scaled, parents included, and that is the point rather than a
    simplification: scaling only the child table would change the average
    fan-out along with the size, so the two worlds would differ in a second way
    and the curve would no longer be about size alone.

    The scaled tables are rebuilt through ``Table``'s own constructor rather
    than assembled behind it, so a declaration that is only valid at its
    original size is refused at the factor that breaks it -- naming the factor,
    because a message about a row count the caller never wrote is a message that
    knows more than it says.
    """
    # A whole number, checked rather than rounded. Rounding would let each table
    # decide its own size at a fractional factor, so two tables in one shape
    # could scale by different real amounts and the fan-out between them would
    # drift for reasons nothing in the declaration mentions.
    if not isinstance(factor, int) or factor < 1:
        raise InvalidShape(
            f"A scale factor multiplies every declared row count, so it has to be a whole "
            f"number of at least 1; got {factor!r}. A world smaller than the declaration is "
            "the declaration written smaller: growth is asserted by scaling up from the "
            "cheapest world that still means something, never by shrinking a larger one."
        )

    try:
        tables = tuple(
            # ``fields`` rather than keyword arguments, because a model may have
            # a column named ``rows`` or ``model``; and ``table.keys`` rather
            # than None, so a strategy the caller passed explicitly survives
            # instead of being re-inferred into a different one.
            Table(
                table.model,
                rows=table.rows * factor,
                fields=dict(table.fields),
                keys=table.keys,
            )
            for table in shape.tables
        )
    except InvalidShape as invalid:
        raise InvalidShape(f"At scale factor {factor}: {invalid}") from invalid

    return Shape(*tables, seed=shape.seed)
