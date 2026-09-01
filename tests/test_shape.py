"""A shape is inert data, and stays that way."""

from __future__ import annotations

import pytest

from django_data_shape import Constant, InvalidShape, Shape, Table
from tests.testapp.models import Company


def _company(rows: int = 1) -> Table:
    return Table(Company, rows=rows, name=Constant("acme"))


def test_it_holds_its_tables_and_seed() -> None:
    shape = Shape(_company(), seed=99)

    assert shape.seed == 99
    assert repr(shape) == "Shape(Company, seed=99)"


def test_the_seed_defaults_to_something_reproducible() -> None:
    # Not random. Two runs of an undeclared seed must still agree, or the
    # package's reproducibility claim holds only for callers who remembered.
    assert Shape(_company()).seed == Shape(_company()).seed


def test_a_shape_with_no_tables_is_refused() -> None:
    with pytest.raises(InvalidShape, match="at least one table"):
        Shape()


def test_the_same_table_declared_twice_is_refused() -> None:
    with pytest.raises(InvalidShape, match="declared twice"):
        Shape(_company(rows=1), _company(rows=2))


def test_it_has_no_build_method() -> None:
    # Building lives in a function so a shape stays hashable, serialisable data:
    # the template-database cache key and the shape-from emitter both need that
    # and neither can have it if a shape can hold a connection.
    assert not hasattr(Shape(_company()), "build")
