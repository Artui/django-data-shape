"""Every remedy a Tree refusal names, loaded on a real table that enforces the uniqueness.

The refusal tests stop at ``Shape()`` on isolated models, so a remedy a message
offers was only ever read, never built: 0.25.1 found a refusal pointing at a
shape that died in ``COPY``. Each test below takes one remedy from one refusal's
wording and builds it on PostgreSQL at several seeds. A passing build is proof
the rows do not collide only because the index is there to refuse a collision,
so each loaded table is made to refuse a copy of one of its own rows.

The tree beside nothing at all has its loads in
``test_build_flat_tree_on_a_unique_key.py``; the tree beside a draw, a fan-out,
a second tree and a conditional uniqueness are here, with the ``Projection`` each
of them offers.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

import django
import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models import F, Model

from django_data_shape import (
    Constant,
    FanOut,
    Projection,
    Sequential,
    Shape,
    Skew,
    Table,
    Tree,
    Zipf,
)
from django_data_shape import build as build_shape
from tests.testapp.models import (
    Category,
    Chain,
    Company,
    Folder,
    LabelledRung,
    Limb,
    Relay,
    RootlessLink,
    RootlessNode,
    Rung,
    Spur,
    Successor,
    TiedLink,
    Verdict,
)

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="the uniqueness is enforced by a PostgreSQL index under COPY",
    ),
]

_SEEDS = [0, 1, 7, 42, 1000]
_ROWS = 200


def _companies() -> Table:
    return Table(Company, rows=50, name=Constant("c"))


def _tree() -> Tree:
    return Tree(roots=0.05, sizes=Zipf())


def _siblings(model: type[Model], column: str) -> int:
    """The most children any one parent holds: above 1, the rows have siblings to collide."""
    parents = [
        parent
        for parent in model._default_manager.values_list(f"{column}_id", flat=True)
        if parent is not None
    ]
    return max(Counter(parents).values())


def _refuses_a_copy_of(model: type[Model], **where: object) -> None:
    """The index that makes a load evidence: a row copied whole is turned away.

    A build that passed on a table with no index would pass for any seed. The
    row chosen has every constrained column set, so the copy collides with it.
    """
    row = model._default_manager.filter(**where).first()
    assert row is not None, "no row sits in the group the index constrains"
    values = {
        field.attname: getattr(row, field.attname)
        for field in model._meta.concrete_fields
        if not field.primary_key
    }
    with pytest.raises(IntegrityError, match="duplicate key"), transaction.atomic():
        model._default_manager.create(**values)


def _keys(model: type[Model], *columns: str) -> Iterable[tuple[object, ...]]:
    return model._default_manager.values_list(*columns)


# A tree beside a draw, in Folder(parent, name).


@pytest.mark.parametrize("seed", _SEEDS)
def test_a_distinct_column_beside_a_tree_of_siblings_loads(seed: int) -> None:
    build_shape(
        Shape(
            Table(Folder, rows=_ROWS, parent=_tree(), name=Sequential(1, 1), code=Constant("c")),
            seed=seed,
        )
    )

    assert Folder.objects.count() == _ROWS
    # Without siblings the distinct column would be doing nothing: the claim is
    # that it keeps the pair apart where the tree gives a parent several rows.
    assert _siblings(Folder, "parent") > 1
    _refuses_a_copy_of(Folder, parent__isnull=False)


@pytest.mark.parametrize("seed", _SEEDS)
def test_sizes_of_one_value_beside_a_draw_load_where_every_sibling_draws_alike(seed: int) -> None:
    build_shape(
        Shape(
            Table(
                Folder,
                rows=_ROWS,
                parent=Tree(roots=0.05, sizes=Constant(1)),
                name=Constant("same"),
                code=Constant("c"),
            ),
            seed=seed,
        )
    )

    assert Folder.objects.count() == _ROWS
    assert _siblings(Folder, "parent") == 1
    assert Folder.objects.values("name").distinct().count() == 1
    _refuses_a_copy_of(Folder, parent__isnull=False)


@pytest.mark.parametrize("seed", _SEEDS)
def test_a_projection_fills_a_tree_of_siblings_beside_a_draw(seed: int) -> None:
    build_shape(
        Shape(
            Projection(
                Folder,
                columns=("id", "parent", "name", "code"),
                sql=(
                    "SELECT g, CASE WHEN g > 1 THEN g / 2 END, 'n' || g, 'c' "
                    "FROM generate_series(1, %s) AS g"
                ),
                params=(_ROWS,),
            ),
            seed=seed,
        )
    )

    assert Folder.objects.count() == _ROWS
    assert _siblings(Folder, "parent") == 2
    _refuses_a_copy_of(Folder, parent__isnull=False)


# A tree alone under a unique self-key, which the refusal tells to leave out.


@pytest.mark.parametrize("model", [Successor, Chain, Relay])
def test_the_tree_left_out_of_a_unique_self_key_loads_and_is_filled_after_the_build(
    model: type[Model],
) -> None:
    build_shape(Shape(Table(model, rows=_ROWS)))

    assert model._default_manager.count() == _ROWS
    assert model._default_manager.filter(previous__isnull=False).count() == 0
    # "Fill it after the build" is a remedy too: a chain the column accepts.
    model._default_manager.filter(pk__gt=1).update(previous_id=F("pk") - 1)
    assert model._default_manager.filter(previous__isnull=False).count() == _ROWS - 1
    _refuses_a_copy_of(model, previous__isnull=False)


@pytest.mark.skipif(
    django.VERSION < (5, 0),
    reason="nulls_distinct arrived in Django 5.0; below it TiedLink is a Chain",
)
def test_a_projection_fills_a_unique_self_key_that_counts_its_nulls_as_one_value() -> None:
    build_shape(
        Shape(
            Projection(
                TiedLink,
                columns=("id", "previous"),
                sql=("SELECT g, CASE WHEN g > 1 THEN g - 1 END FROM generate_series(1, %s) AS g"),
                params=(_ROWS,),
            )
        )
    )

    links = dict(_keys(TiedLink, "pk", "previous_id"))
    assert len(links) == _ROWS
    assert [key for key, parent in links.items() if parent is None] == [1]
    _refuses_a_copy_of(TiedLink, previous__isnull=False)


@pytest.mark.skipif(django.VERSION < (5, 0), reason="nulls_distinct arrived in Django 5.0")
def test_the_tree_left_out_of_a_self_key_that_ties_its_nulls_is_not_a_remedy() -> None:
    # The refusal does not offer this, and here is why: every row would hold the
    # one NULL the constraint allows once.
    with pytest.raises(Exception, match="duplicate key"):  # noqa: B017
        build_shape(Shape(Table(TiedLink, rows=_ROWS)))


# A self-key that refuses NULL, which no tree can fill.


@pytest.mark.parametrize("seed", _SEEDS)
def test_the_nullable_key_a_refusal_asks_for_loads_a_tree(seed: int) -> None:
    # RootlessNode.parent is refused a Tree for being non-null; Category is the
    # same table with null=True, which is the first remedy the message names.
    build_shape(Shape(Table(Category, rows=_ROWS, name=Constant("c"), parent=_tree()), seed=seed))

    assert Category.objects.count() == _ROWS
    assert Category.objects.filter(parent__isnull=True).count() == round(_ROWS * 0.05)
    assert _siblings(Category, "parent") > 1


def test_a_projection_fills_a_self_key_that_refuses_null() -> None:
    build_shape(
        Shape(
            Projection(
                RootlessNode,
                columns=("id", "parent"),
                sql="SELECT g, (g + 1) / 2 FROM generate_series(1, %s) AS g",
                params=(_ROWS,),
            )
        )
    )

    assert RootlessNode.objects.count() == _ROWS
    assert RootlessNode.objects.get(pk=1).parent_id == 1


def test_a_projection_fills_a_unique_self_key_that_refuses_null() -> None:
    build_shape(
        Shape(
            Projection(
                RootlessLink,
                columns=("id", "previous"),
                sql="SELECT g, g %% %s + 1 FROM generate_series(1, %s) AS g",
                params=(_ROWS, _ROWS),
            )
        )
    )

    assert RootlessLink.objects.count() == _ROWS
    _refuses_a_copy_of(RootlessLink)


# A tree beside a fan-out, in Limb(parent, company).


@pytest.mark.parametrize("seed", _SEEDS)
def test_a_fan_out_capped_at_one_child_per_company_loads_beside_a_tree_of_siblings(
    seed: int,
) -> None:
    build_shape(
        Shape(
            Table(Company, rows=300, name=Constant("c")),
            Table(
                Limb,
                rows=_ROWS,
                parent=_tree(),
                company=FanOut(Zipf(), max_children=1),
            ),
            seed=seed,
        )
    )

    assert Limb.objects.count() == _ROWS
    assert _siblings(Limb, "parent") > 1
    assert _siblings(Limb, "company") == 1
    _refuses_a_copy_of(Limb, parent__isnull=False, company__isnull=False)


@pytest.mark.parametrize("seed", _SEEDS)
def test_sizes_of_one_value_load_beside_a_skewed_fan_out(seed: int) -> None:
    build_shape(
        Shape(
            _companies(),
            Table(
                Limb,
                rows=_ROWS,
                parent=Tree(roots=0.05, sizes=Constant(1)),
                company=FanOut(Zipf()),
            ),
            seed=seed,
        )
    )

    assert Limb.objects.count() == _ROWS
    assert _siblings(Limb, "parent") == 1
    # The fan-out still repeats a company: the tree alone is what keeps the pair apart.
    assert _siblings(Limb, "company") > 1
    _refuses_a_copy_of(Limb, parent__isnull=False, company__isnull=False)


@pytest.mark.parametrize("seed", _SEEDS)
def test_the_tree_left_out_loads_beside_a_skewed_fan_out(seed: int) -> None:
    build_shape(Shape(_companies(), Table(Limb, rows=_ROWS, company=FanOut(Zipf())), seed=seed))

    assert Limb.objects.count() == _ROWS
    assert Limb.objects.filter(parent__isnull=False).count() == 0
    assert _siblings(Limb, "company") > 1


@pytest.mark.parametrize("seed", _SEEDS)
def test_a_projection_fills_a_tree_beside_a_fan_out(seed: int) -> None:
    build_shape(
        Shape(
            _companies(),
            Projection(
                Limb,
                columns=("id", "parent", "company"),
                sql=(
                    "SELECT g, CASE WHEN g > 1 THEN g / 2 END, g %% 50 + 1 "
                    "FROM generate_series(1, %s) AS g"
                ),
                params=(_ROWS,),
                reads=(Company,),
            ),
            seed=seed,
        )
    )

    assert Limb.objects.count() == _ROWS
    assert _siblings(Limb, "parent") == 2
    _refuses_a_copy_of(Limb, parent__isnull=False, company__isnull=False)


# A tree and a fan-out beside a third column, in Spur(parent, company, label).


@pytest.mark.parametrize("seed", _SEEDS)
def test_a_distinct_column_loads_beside_a_tree_and_a_fan_out(seed: int) -> None:
    build_shape(
        Shape(
            _companies(),
            Table(
                Spur,
                rows=_ROWS,
                parent=_tree(),
                company=FanOut(Zipf()),
                label=Sequential(1, 1),
            ),
            seed=seed,
        )
    )

    assert Spur.objects.count() == _ROWS
    assert _siblings(Spur, "parent") > 1
    assert _siblings(Spur, "company") > 1
    _refuses_a_copy_of(Spur, parent__isnull=False, company__isnull=False, label__isnull=False)


# Two trees, in Rung(left, right) and LabelledRung(left, right, label).


@pytest.mark.parametrize("seed", _SEEDS)
def test_sizes_of_one_value_in_one_of_two_trees_load(seed: int) -> None:
    build_shape(
        Shape(
            Table(
                Rung,
                rows=_ROWS,
                left=Tree(roots=0.05, sizes=Constant(1)),
                right=_tree(),
            ),
            seed=seed,
        )
    )

    assert Rung.objects.count() == _ROWS
    assert _siblings(Rung, "left") == 1
    assert _siblings(Rung, "right") > 1
    _refuses_a_copy_of(Rung, left__isnull=False, right__isnull=False)


@pytest.mark.parametrize("seed", _SEEDS)
def test_the_second_tree_left_out_loads(seed: int) -> None:
    build_shape(Shape(Table(Rung, rows=_ROWS, left=_tree()), seed=seed))

    assert Rung.objects.count() == _ROWS
    assert Rung.objects.filter(right__isnull=False).count() == 0
    assert _siblings(Rung, "left") > 1


@pytest.mark.parametrize("seed", _SEEDS)
def test_a_projection_fills_two_trees(seed: int) -> None:
    build_shape(
        Shape(
            Projection(
                Rung,
                columns=("id", "left", "right"),
                sql=(
                    "SELECT g, CASE WHEN g > 1 THEN g / 2 END, CASE WHEN g > 1 AND g %% 2 = 1 THEN g / 2 END "
                    "FROM generate_series(1, %s) AS g"
                ),
                params=(_ROWS,),
            ),
            seed=seed,
        )
    )

    assert Rung.objects.count() == _ROWS
    assert _siblings(Rung, "left") == 2
    _refuses_a_copy_of(Rung, left__isnull=False, right__isnull=False)


@pytest.mark.parametrize("seed", _SEEDS)
def test_a_distinct_column_loads_beside_two_trees(seed: int) -> None:
    build_shape(
        Shape(
            Table(
                LabelledRung,
                rows=_ROWS,
                left=_tree(),
                right=_tree(),
                label=Sequential(1, 1),
            ),
            seed=seed,
        )
    )

    assert LabelledRung.objects.count() == _ROWS
    assert _siblings(LabelledRung, "left") > 1
    assert _siblings(LabelledRung, "right") > 1
    _refuses_a_copy_of(LabelledRung, left__isnull=False, right__isnull=False, label__isnull=False)


# A tree in a conditional uniqueness, in Verdict(parent, code) where status is accepted.


def _shares_a_code_among_siblings() -> bool:
    pairs = [
        (parent, code) for parent, code in _keys(Verdict, "parent_id", "code") if parent is not None
    ]
    return max(Counter(pairs).values()) > 1


@pytest.mark.parametrize("seed", _SEEDS)
def test_a_condition_no_row_lands_inside_loads_whatever_the_siblings_draw(seed: int) -> None:
    build_shape(
        Shape(
            Table(
                Verdict,
                rows=_ROWS,
                parent=_tree(),
                code=Skew({"a": 1, "b": 1}),
                status=Skew({"open": 1, "closed": 1}),
            ),
            seed=seed,
        )
    )

    assert Verdict.objects.count() == _ROWS
    assert Verdict.objects.filter(status="accepted").count() == 0
    # Siblings do share a code: the condition is what keeps them apart.
    assert _shares_a_code_among_siblings()
    # The index is partial, so a copy is turned away only once it is accepted.
    first = Verdict.objects.filter(parent__isnull=False).first()
    assert first is not None
    Verdict.objects.filter(pk=first.pk).update(status="accepted")
    with pytest.raises(IntegrityError, match="duplicate key"), transaction.atomic():
        Verdict.objects.create(parent_id=first.parent_id, code=first.code, status="accepted")


@pytest.mark.parametrize("seed", _SEEDS)
def test_a_distinct_column_loads_with_every_row_inside_the_condition(seed: int) -> None:
    build_shape(
        Shape(
            Table(
                Verdict,
                rows=_ROWS,
                parent=_tree(),
                code=Sequential(1, 1),
                status=Constant("accepted"),
            ),
            seed=seed,
        )
    )

    assert Verdict.objects.filter(status="accepted").count() == _ROWS
    assert _siblings(Verdict, "parent") > 1
    _refuses_a_copy_of(Verdict, parent__isnull=False, status="accepted")


@pytest.mark.parametrize("seed", _SEEDS)
def test_sizes_of_one_value_load_with_every_row_inside_the_condition(seed: int) -> None:
    build_shape(
        Shape(
            Table(
                Verdict,
                rows=_ROWS,
                parent=Tree(roots=0.05, sizes=Constant(1)),
                code=Constant("c"),
                status=Constant("accepted"),
            ),
            seed=seed,
        )
    )

    assert Verdict.objects.filter(status="accepted").count() == _ROWS
    assert _siblings(Verdict, "parent") == 1
    _refuses_a_copy_of(Verdict, parent__isnull=False, status="accepted")


@pytest.mark.parametrize("seed", _SEEDS)
def test_a_projection_fills_a_tree_in_a_conditional_uniqueness(seed: int) -> None:
    build_shape(
        Shape(
            Projection(
                Verdict,
                columns=("id", "parent", "code", "status"),
                sql=(
                    "SELECT g, CASE WHEN g > 1 THEN g / 2 END, 'c' || g, 'accepted' "
                    "FROM generate_series(1, %s) AS g"
                ),
                params=(_ROWS,),
            ),
            seed=seed,
        )
    )

    assert Verdict.objects.filter(status="accepted").count() == _ROWS
    assert _siblings(Verdict, "parent") == 2
    _refuses_a_copy_of(Verdict, parent__isnull=False, status="accepted")
