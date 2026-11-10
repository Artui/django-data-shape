from __future__ import annotations

from collections import Counter

import pytest
from django.db import connection

import django_data_shape
from django_data_shape import Constant, Shape, Table, Zipf
from django_data_shape import build as build_shape
from tests.testapp.models import Folder, Referred

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="COPY loading and planner statistics need PostgreSQL",
    ),
]


def _tree(**kwargs: object) -> object:
    tree = getattr(django_data_shape, "Tree", None)
    assert tree is not None, "django_data_shape exports no Tree declaration"
    return tree(**kwargs)


def _parents() -> dict[int, int | None]:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT id, referrer_id FROM {Referred._meta.db_table}")
        return dict(cursor.fetchall())


def test_a_self_referential_foreign_key_builds_an_acyclic_tree_with_its_root_share() -> None:
    build_shape(
        Shape(
            Table(
                Referred,
                rows=2_000,
                label=Constant("c"),
                referrer=_tree(roots=0.05, sizes=Zipf()),
            ),
            seed=11,
        )
    )
    parents = _parents()
    assert len(parents) == 2_000
    roots = [key for key, parent in parents.items() if parent is None]
    assert len(roots) == 100
    # ceil(2000 * 0.05): the root count is exact, not a draw that lands near it.
    assert all(parent < key for key, parent in parents.items() if parent is not None)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            WITH RECURSIVE climb(id, referrer_id, depth) AS (
                SELECT id, referrer_id, 0 FROM {Referred._meta.db_table}
                UNION ALL
                SELECT climb.id, node.referrer_id, climb.depth + 1
                FROM climb JOIN {Referred._meta.db_table} node ON node.id = climb.referrer_id
                WHERE climb.depth < 2000
            )
            SELECT max(depth) FROM climb
            """
        )
        deepest = cursor.fetchone()[0]
    assert deepest < 2_000


def test_the_parent_column_climbs_with_the_table_as_the_rule_makes_it() -> None:
    """The statistic docs/relations.md states, and why it is not a flattering one."""
    build_shape(
        Shape(
            Table(
                Referred, rows=5_000, label=Constant("c"), referrer=_tree(roots=0.05, sizes=Zipf())
            ),
            seed=1,
        )
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT correlation, null_frac FROM pg_stats WHERE tablename = %s AND attname = %s",
            [Referred._meta.db_table, "referrer_id"],
        )
        correlation, null_frac = cursor.fetchone()
    assert correlation > 0.7
    assert null_frac == pytest.approx(0.05)


def test_the_children_are_not_gathered_under_the_lowest_keys() -> None:
    """Weight is keyed on a row's position, so the families sit where it fell."""
    build_shape(
        Shape(
            Table(
                Referred,
                rows=5_000,
                label=Constant("c"),
                referrer=_tree(roots=0.05, sizes=Zipf()),
            ),
            seed=1,
        )
    )
    families = Counter(parent for parent in _parents().values() if parent is not None)
    children = sum(families.values())
    first_tenth = sum(count for key, count in families.items() if key <= 500)
    last_half = sum(count for key, count in families.items() if key > 2_500)
    assert first_tenth / children < 0.25
    assert last_half / children > 0.33


class _Words:
    def value(self, row: int, draw: float) -> object:
        return "many"

    def canonical(self) -> object:
        return ("words",)


def test_sizes_refused_inside_the_load_leave_the_connection_usable() -> None:
    # It still arrives as the refusal, with the load rolled back and the connection still usable.
    with pytest.raises(django_data_shape.InvalidShape, match="needs numeric tree sizes"):
        build_shape(
            Shape(
                Table(
                    Referred,
                    rows=20,
                    label=Constant("c"),
                    referrer=_tree(roots=0.1, sizes=_Words()),
                )
            )
        )
    assert Referred.objects.count() == 0


@pytest.mark.parametrize("seed", range(5))
def test_a_flat_tree_beside_a_draw_loads_under_its_uniqueness(seed: int) -> None:
    # No row has two children, which is the claim the declaration check stands on.
    build_shape(
        Shape(
            Table(
                Folder,
                rows=300,
                parent=_tree(roots=0.05, sizes=Constant(1)),
                name=Constant("same"),
                code=Constant("c"),
            ),
            seed=seed,
        )
    )

    assert Folder.objects.count() == 300
