from __future__ import annotations

import pytest
from django.db import connection

from django_data_shape import Constant, Shape, Table, build, scaled_world
from tests.testapp.models import Company

pytestmark = pytest.mark.django_db


def _world(rows: int) -> Shape:
    return Shape(Table(Company, rows=rows, name=Constant("acme")))


def test_the_sequence_matches_the_rows_that_came_back() -> None:
    """The regression: a small scaled world over a large session world."""
    build(_world(50), require_statistics=False)

    with scaled_world(_world(5), 1, using=connection.alias):
        assert Company.objects.count() == 5

    assert Company.objects.count() == 50
    created = Company.objects.create(name="after")

    assert created.pk > 50


def test_the_sequence_is_usable_when_the_world_was_the_only_thing_there() -> None:
    """The ordinary case, unchanged: nothing was there, nothing comes back."""
    with scaled_world(_world(5), 1, using=connection.alias):
        assert Company.objects.count() == 5

    assert Company.objects.count() == 0

    created = Company.objects.create(name="after")

    assert created.pk >= 1


def test_a_larger_scaled_world_also_leaves_a_usable_sequence() -> None:
    """The direction that happened to work before, pinned so it keeps working."""
    build(_world(5), require_statistics=False)

    with scaled_world(_world(50), 1, using=connection.alias):
        assert Company.objects.count() == 50

    assert Company.objects.count() == 5

    created = Company.objects.create(name="after")

    assert created.pk > 5
