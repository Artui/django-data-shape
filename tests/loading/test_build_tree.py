"""Building a self-referential tree: a foreign key that points at its own table.

``Tree`` is reached through the package's namespace inside each test rather
than imported at the top, and the model is one main already has, so that against
a tree without the feature this module still collects and fails on its
assertions -- a module that dies at import proves nothing about the feature.
"""

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
    # Committed by the transactional test, so the deferred foreign keys were
    # checked at commit: every parent named is a row of the table.
    parents = _parents()
    assert len(parents) == 2_000
    roots = [key for key, parent in parents.items() if parent is None]
    # ceil(2000 * 0.05): the root count is exact, not a draw that lands near it.
    assert len(roots) == 100
    # Acyclic by construction: every parent key is below its child's key, so
    # following parents strictly descends and has to stop at a root.
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
    """The statistic docs/relations.md states, and why it is not a flattering one.

    Every parent is an earlier row, so the parent column can only climb as the
    table goes on: ``pg_stats.correlation`` comes out high. That is what a
    production tree looks like too, a row being inserted after the row it
    hangs from, so it is the honest value rather than the clustered layout a
    fan-out's ``placement="grouped"`` produces.
    """
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
    """Weight is keyed on a row's position, so the families sit where it fell.

    The obvious construction -- each child picks an earlier row by weight --
    makes the first rows the largest parents purely because every later row
    could pick them. Measured over seeds 0 to 7 of this declaration it puts
    28% to 48% of the children under the first tenth of the keys and 11% to 22%
    under the last half; this one puts 10% to 23% and 39% to 50%, against a fair
    share of 10% and 50%. A single ``Zipf`` whale can hold a fifth of the table,
    which is why the bounds are wide. What is left of the gap is the rule's
    price rather than the construction's: the last rows have too few rows after
    them to take the children their weights ask for.
    """
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
    # A tree is resolved inside generation, so a bad size is refused from
    # inside COPY rather than before it, as a fan-out's is. It still arrives as
    # the refusal, with the load rolled back and the connection still usable.
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
    # One name for every row collides among any siblings at all, so a load here
    # is the claim the declaration check stands on: no row has two children.
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
