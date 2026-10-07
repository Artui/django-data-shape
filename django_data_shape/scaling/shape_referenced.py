"""Raised when a scaled world would have to remove rows something else references."""

from __future__ import annotations


class ShapeReferenced(Exception):
    """Rows the world did not make reference rows it would have to remove.

    A scaled world empties the tables its shape declares before building, and
    removes their rows and nothing else. When a row in some other table holds a
    foreign key to one of those rows, there is no way to do both: removing the
    row leaves the reference pointing at nothing, or takes the referencing row
    with it through an ``ON DELETE``, and either changes a table the shape does
    not declare. So the world refuses, before it removes anything, and names
    each reference as ``referencing_table.column -> declared_table``.

    Its own type rather than
    :class:`~django_data_shape.loading.shape_not_empty.ShapeNotEmpty`, because
    the remedy differs: a scaled world empties a declared table that holds rows,
    so rows alone are never the problem here. What is wrong is a row outside the
    declaration that depends on them. The message gives the three ways out --
    declare the referencing table too, so its rows are the world's; give the
    declared table :class:`~django_data_shape.keys.disjoint.Disjoint` keys, so
    the world builds beside the rows already there rather than emptying it; or
    do not create the referencing rows in that test.

    Raised inside the world's own transaction and before any row is removed,
    so a world refused this way has changed nothing.
    """
