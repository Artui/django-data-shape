"""A tree built where there is no COPY: SQLite, through the portable route.

Every parent is an earlier row, so it is already in the table when its child
is inserted. Checked with SQLite's own foreign key check rather than trusted,
because a test transaction is never committed and a deferred key is never
checked otherwise.
"""

from __future__ import annotations

import pytest
from django.db import connections

from django_data_shape import Constant, Shape, Table, Tree, Zipf, build
from tests.testapp.models import Category

pytestmark = pytest.mark.django_db(databases=["default", "not_postgres"])

_ALIAS = "not_postgres"


def test_a_tree_loads_on_the_portable_route_with_every_parent_present() -> None:
    build(
        Shape(
            Table(Category, rows=300, name=Constant("c"), parent=Tree(roots=0.1, sizes=Zipf())),
            seed=9,
        ),
        using=_ALIAS,
        require_statistics=False,
    )

    rows = dict(Category.objects.using(_ALIAS).values_list("pk", "parent_id"))
    assert len(rows) == 300
    assert sum(parent is None for parent in rows.values()) == 30
    assert all(parent < key for key, parent in rows.items() if parent is not None)
    with connections[_ALIAS].cursor() as cursor:
        cursor.execute(f"PRAGMA foreign_key_check({Category._meta.db_table})")
        assert cursor.fetchall() == []
