"""Turning a declared table into the tuples COPY will consume."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from django_data_shape.table import Table
from django_data_shape.utils import draw, field_stream


def generate_rows(table: Table, seed: int) -> Iterator[tuple[Any, ...]]:
    """Yield one tuple per row: the primary key, then each declared column.

    Rows, not model instances. The ORM is the wrong tool at these counts -- it
    is the difference between a load measured in seconds and one measured in
    minutes -- and nothing here needs a model instance, because no ``save`` will
    run and no signal should fire.

    Primary keys are a dense ``1..N`` because this package assigns them. That is
    what will let a child row's foreign key be satisfied by construction with no
    lookup, and it is why a self-referential tree is acyclic for free. It also
    obliges the caller to reset the sequence afterwards; see ``build``.

    A generator rather than a list: a million rows of tuples is real memory, and
    psycopg writes them one at a time anyway, so materialising the whole set
    would buy nothing but peak RSS.
    """
    columns = table.columns()
    # Stream ids are derived once per field rather than per row. At a million
    # rows this loop runs a million times per column, so anything hoistable out
    # of it is worth hoisting.
    streams = [field_stream(seed, table.db_table, name) for name, _ in columns]
    distributions = [table.fields[name] for name, _ in columns]

    for row in range(table.rows):
        yield (
            row + 1,
            *(
                distribution.value(row, draw(stream, row))
                for distribution, stream in zip(distributions, streams, strict=True)
            ),
        )
