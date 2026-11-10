"""A flat tree loading into a unique self-key, on the database that enforces it.

The declaration tests stop at ``Shape()``. This is the load an exemption is owed:
0.25.1 found an exemption accepted by arithmetic and dying in ``COPY``, so every
spelling of the uniqueness builds here at several seeds, and a duplicate is then
inserted to show that the index was there to refuse one.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models import Model

from django_data_shape import Constant, InvalidShape, Shape, Skew, Table, Tree, Zipf
from django_data_shape import build as build_shape
from django_data_shape.distributions.distribution import Distribution
from django_data_shape.scaling.scaled_shape import scaled_shape
from tests.testapp.models import Chain, Relay, Successor

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="the uniqueness is enforced by a PostgreSQL index under COPY",
    ),
]

_SPELLINGS = [Successor, Chain, Relay]


def _links(model: type[Model]) -> dict[int, int | None]:
    return dict(model._default_manager.values_list("pk", "previous_id"))


@pytest.mark.parametrize("model", _SPELLINGS)
@pytest.mark.parametrize("seed", [0, 1, 7, 42, 1000])
@pytest.mark.parametrize("roots", [0.05, 0.5])
def test_a_flat_tree_builds_on_a_unique_self_key(
    model: type[Model], seed: int, roots: float
) -> None:
    build_shape(
        Shape(Table(model, rows=200, previous=Tree(roots=roots, sizes=Constant(1))), seed=seed)
    )

    links = _links(model)
    parents = [parent for parent in links.values() if parent is not None]
    assert len(links) == 200
    assert len(parents) == 200 - round(200 * roots)
    assert len(parents) == len(set(parents))
    assert all(parent < key for key, parent in links.items() if parent is not None)


@pytest.mark.parametrize("model", _SPELLINGS)
@pytest.mark.parametrize("sizes", [Constant(7), Skew({3: 1})], ids=["constant", "skew_of_one"])
def test_any_one_size_builds_since_only_the_spread_is_weighed(
    model: type[Model], sizes: Distribution
) -> None:
    build_shape(Shape(Table(model, rows=200, previous=Tree(roots=0.05, sizes=sizes))))

    parents = [parent for parent in _links(model).values() if parent is not None]
    assert len(parents) == len(set(parents))


@pytest.mark.parametrize("model", _SPELLINGS)
@pytest.mark.parametrize("seed", [0, 1, 7])
def test_one_child_builds_whatever_the_sizes_weigh(model: type[Model], seed: int) -> None:
    build_shape(Shape(Table(model, rows=2, previous=Tree(roots=0.5, sizes=Zipf())), seed=seed))

    assert _links(model) == {1: None, 2: 1}


@pytest.mark.parametrize("model", _SPELLINGS)
def test_a_scaled_flat_tree_still_keeps_the_uniqueness(model: type[Model]) -> None:
    flat = Shape(Table(model, rows=50, previous=Tree(roots=0.05, sizes=Constant(1))))

    build_shape(scaled_shape(flat, 4))

    parents = [parent for parent in _links(model).values() if parent is not None]
    assert len(parents) == 190
    assert len(parents) == len(set(parents))


@pytest.mark.parametrize("model", [Successor, Chain, Relay])
def test_a_scale_that_gives_a_row_siblings_is_refused_by_the_copy_it_makes(
    model: type[Model],
) -> None:
    # One child of a skew has no sibling at two rows, and has at twenty, so the
    # proof is read again rather than carried over from the smaller table.
    small = Shape(Table(model, rows=2, previous=Tree(roots=0.5, sizes=Zipf())))

    with pytest.raises(InvalidShape, match="sizes=Constant"):
        scaled_shape(small, 10)


@pytest.mark.parametrize("model", _SPELLINGS)
def test_the_index_a_flat_tree_loaded_past_refuses_a_second_child(model: type[Model]) -> None:
    # What makes the builds above evidence: a tree that broke the rule would have
    # met this index inside COPY.
    build_shape(Shape(Table(model, rows=50, previous=Tree(roots=0.05, sizes=Constant(1)))))
    taken = next(parent for parent in _links(model).values() if parent is not None)

    with pytest.raises(IntegrityError, match="duplicate key"), transaction.atomic():
        model._default_manager.create(previous_id=taken)


@pytest.mark.parametrize("model", _SPELLINGS)
def test_a_flat_tree_of_no_weight_is_refused_before_any_row(model: type[Model]) -> None:
    shape = Shape(Table(model, rows=50, previous=Tree(roots=0.05, sizes=Constant(0))))

    with pytest.raises(InvalidShape, match="sum to 0.0"):
        build_shape(shape)
