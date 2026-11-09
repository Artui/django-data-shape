"""A conditional uniqueness whose condition names a relation column.

A relation's declaration says which keys a column can hold, and it used to be compared
to the condition's value as though it were that value, which is never true, so every
one of these read as never inside the condition and was accepted.

**A figure here is only true of its table's name**, because the draws are keyed on it.
Each ``loads`` comment below was measured over twenty seeds on the table these cases
make, with the check switched off. The sizes are chosen so that the figure means
something: ``Q(owner=1)`` over three hundred people loaded twenty times in twenty on a
table where ten people loaded once, because the one key had few rows to collide.
"""

from __future__ import annotations

from typing import Any, NamedTuple, cast

import django
import pytest
from django.db import connection, models
from django.db.models import Q, UniqueConstraint
from django.test.utils import isolate_apps

from django_data_shape import (
    Constant,
    Derived,
    FanOut,
    InvalidShape,
    Paired,
    PerParent,
    Projection,
    Sequential,
    Shape,
    Skew,
    Table,
    Tree,
    Uniform,
    Zipf,
)
from django_data_shape import build as build_shape
from tests.testapp.models import Company, Person

_NEEDS_DJANGO_5 = pytest.mark.skipif(
    django.VERSION < (5, 0), reason="db_default and nulls_distinct arrived in Django 5.0"
)
_ON_POSTGRES = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="the uniqueness is enforced by a PostgreSQL index under COPY",
)


class _Case(NamedTuple):
    name: str
    fields: tuple[str, ...]
    condition: Q
    columns: dict[str, Any]
    rows: int = 200
    companies: int = 50
    people: int | None = 10
    together: bool = False
    owner_required: bool = False
    owner_db_default: int | None = None


def _model(case: _Case) -> type[models.Model]:
    """One table per case, because the draws are keyed on the table's name."""
    tied: dict[str, Any] = {"nulls_distinct": False} if case.together else {}
    owner: dict[str, Any] = {"null": not case.owner_required}
    if case.owner_db_default is not None:
        owner["db_default"] = case.owner_db_default
    with isolate_apps("tests.testapp"):
        meta = type(
            "Meta",
            (),
            {
                "app_label": "testapp",
                "constraints": [
                    UniqueConstraint(
                        fields=list(case.fields),
                        condition=case.condition,
                        name=f"uq_{case.name}"[:60],
                        **tied,
                    )
                ],
            },
        )
        return cast(
            "type[models.Model]",
            type(
                case.name,
                (models.Model,),
                {
                    "__module__": __name__,
                    "company": models.ForeignKey(
                        Company, null=True, on_delete=models.CASCADE, related_name="+"
                    ),
                    "owner": models.ForeignKey(
                        Person, on_delete=models.CASCADE, related_name="+", **owner
                    ),
                    "other": models.ForeignKey(
                        Company, null=True, on_delete=models.CASCADE, related_name="+"
                    ),
                    "prev": models.ForeignKey(
                        "self", null=True, on_delete=models.CASCADE, related_name="+"
                    ),
                    "label": models.CharField(max_length=20, null=True),
                    "state": models.CharField(max_length=20, null=True),
                    "Meta": meta,
                },
            ),
        )


def _tables(case: _Case, model: type[models.Model]) -> tuple[Table, ...]:
    parents = [Table(Company, rows=case.companies, name=Constant("acme"))]
    if case.people is not None:
        parents.append(Table(Person, rows=case.people, name=Constant("pat")))
    return (*parents, Table(model, rows=case.rows, **case.columns))


def _refusal(case: _Case) -> str:
    with pytest.raises(InvalidShape) as raised:
        Shape(*_tables(case, _model(case)))
    return str(raised.value)


def _assert_loads(case: _Case) -> None:
    """Accepted at declaration, and it loads at several seeds on a real unique index."""
    model = _model(case)
    tables = _tables(case, model)
    names = ", ".join(connection.ops.quote_name(one.model._meta.db_table) for one in tables)
    with connection.schema_editor() as editor:
        editor.create_model(model)
    try:
        for seed in (0, 1, 7, 42, 1000):
            build_shape(Shape(*tables, seed=seed))
            with connection.cursor() as cursor:
                cursor.execute(f"TRUNCATE {names} CASCADE")
    finally:
        with connection.schema_editor() as editor:
            editor.delete_model(model)


def _named_for_position(group: object) -> str:
    """A value per position inside a group, which is what keeps a uniqueness over it."""
    position, _size = cast("tuple[int, int]", group)
    return f"at-{position}"


def _skew() -> Skew:
    return Skew({"a": 1, "b": 1, "c": 1})


def _paired(**narrowed: Any) -> Paired:
    return Paired("company", Zipf(), **narrowed)


def _flat(**declared: Any) -> FanOut:
    return FanOut(Constant(1), **declared)


def _tree() -> Tree:
    return Tree(roots=0.05, sizes=Zipf())


def _flat_tree() -> Tree:
    return Tree(roots=0.05, sizes=Constant(1))


def _params(cases: tuple[_Case, ...]) -> list[Any]:
    return [
        pytest.param(case, id=case.name, marks=[_NEEDS_DJANGO_5] if case.together else [])
        for case in cases
    ]


_REFUSED = (
    # A key over a fan-out's parents. Refused at any size, because what size changes is
    # the odds, and a table small enough to load every time is no proof either.
    # Loads 16 of 20.
    _Case(
        "key_at_twenty_rows",
        ("company",),
        Q(owner=1),
        {"company": FanOut(Zipf()), "owner": FanOut(Zipf())},
        rows=20,
    ),
    # Loads 0 of 20.
    _Case(
        "key_over_a_fan_out",
        ("company",),
        Q(owner=1),
        {"company": FanOut(Zipf()), "owner": FanOut(Zipf())},
    ),
    # Loads 1 of 20.
    _Case(
        "key_by_its_attname",
        ("company",),
        Q(owner_id=1),
        {"company": FanOut(Zipf()), "owner": FanOut(Zipf())},
    ),
    # Loads 0 of 20.
    _Case(
        "key_in_parents",
        ("company",),
        Q(owner=1),
        {"company": FanOut(Zipf()), "owner": FanOut(Zipf(), parents=(1, 2))},
    ),
    # Loads 0 of 20.
    _Case(
        "key_over_too_few_for_flat_sizes",
        ("company",),
        Q(owner=1),
        {"company": FanOut(Zipf()), "owner": _flat()},
    ),
    # Loads 0 of 20: a key written as text is one key to the database.
    _Case(
        "key_written_as_text",
        ("company",),
        Q(owner="1"),
        {"company": FanOut(Zipf()), "owner": FanOut(Zipf(), parents=(1, 2))},
    ),
    # A null share under the condition.
    # Loads 0 of 20.
    _Case(
        "null_share",
        ("company",),
        Q(owner=None),
        {"company": FanOut(Zipf()), "owner": FanOut(Zipf(), null=0.3)},
    ),
    # Loads 0 of 20.
    _Case(
        "null_share_under_a_cap",
        ("company",),
        Q(owner=None),
        {"company": FanOut(Zipf()), "owner": FanOut(Zipf(), max_children=1, null=0.3)},
        people=300,
    ),
    # Loads 0 of 20.
    _Case(
        "null_share_grouped_together",
        ("owner",),
        Q(owner=None),
        {"owner": FanOut(Zipf(), null=0.3)},
        together=True,
    ),
    # Loads 0 of 20.
    _Case(
        "null_beside_a_draw_grouped_together",
        ("owner", "label"),
        Q(owner=None),
        {"owner": FanOut(Zipf(), null=0.3), "label": _skew()},
        together=True,
    ),
    # The column's own fan-out: every row inside the condition shares that key.
    # Loads 7 of 20.
    _Case(
        "own_fan_out_alone",
        ("company",),
        Q(company=1),
        {"company": FanOut(Zipf())},
    ),
    # Loads 16 of 20.
    _Case(
        "own_fan_out_beside_a_draw",
        ("company", "label"),
        Q(company=1),
        {"company": FanOut(Zipf()), "label": _skew()},
    ),
    # Loads 0 of 20.
    _Case(
        "own_key_or_null",
        ("owner",),
        Q(owner=1) | Q(owner=None),
        {"owner": FanOut(Zipf(), null=0.3)},
    ),
    # A set: a cap of one leaves each key one row, and two keys two rows, which can share
    # a group.
    # Loads 10 of 20.
    _Case(
        "set_under_a_cap",
        ("company",),
        Q(owner__in=[1, 2]),
        {"company": FanOut(Zipf()), "owner": FanOut(Zipf(), max_children=1)},
        companies=5,
        people=300,
    ),
    # A tree under its condition.
    # Loads 17 of 20.
    _Case(
        "key_over_a_tree",
        ("company",),
        Q(prev=1),
        {"company": FanOut(Zipf()), "prev": _tree()},
    ),
    # Loads 10 of 20.
    _Case(
        "key_over_a_tree_alone",
        ("prev",),
        Q(prev=1),
        {"prev": _tree()},
    ),
    # Loads 15 of 20.
    _Case(
        "key_over_a_tree_beside_a_fan_out",
        ("prev", "company"),
        Q(prev=1),
        {"company": FanOut(Zipf()), "prev": _tree()},
    ),
    # Loads 5 of 20.
    _Case(
        "fan_out_key_under_a_tree",
        ("prev",),
        Q(owner=1),
        {"prev": _tree(), "owner": FanOut(Zipf())},
    ),
    # Loads 6 of 20.
    _Case(
        "roots_under_a_fan_out",
        ("company",),
        Q(prev=None),
        {"company": FanOut(Zipf()), "prev": _tree()},
    ),
    # Loads 4 of 20.
    _Case(
        "roots_of_a_flat_tree",
        ("company",),
        Q(prev=None),
        {"company": FanOut(Zipf()), "prev": _flat_tree()},
    ),
    # Loads 16 of 20.
    _Case(
        "key_over_one_root",
        ("company",),
        Q(prev=1),
        {"company": FanOut(Zipf()), "prev": Tree(roots=1 / 200, sizes=Zipf())},
    ),
    # Loads 18 of 20.
    _Case(
        "set_over_a_flat_tree",
        ("company",),
        Q(prev__in=[1, 2]),
        {"company": FanOut(Zipf()), "prev": _flat_tree()},
        companies=5,
    ),
    # Loads 0 of 20.
    _Case(
        "roots_grouped_together",
        ("prev",),
        Q(prev=None),
        {"prev": _tree()},
        together=True,
    ),
    # A Paired: its partners are distinct inside its own relation's groups and nowhere else.
    # Loads 11 of 20.
    _Case(
        "paired_set_over_its_relation",
        ("company",),
        Q(owner__in=[1, 2, 3]),
        {"company": _flat(), "owner": _paired()},
        people=30,
    ),
    # Loads 11 of 20.
    _Case(
        "paired_under_another_fan_out",
        ("other",),
        Q(owner=1),
        {"company": _flat(), "other": FanOut(Zipf()), "owner": _paired()},
        people=30,
    ),
    # Loads 13 of 20: a Paired beside some other fan-out is no pair of the pairing's.
    _Case(
        "paired_beside_another_fan_out",
        ("owner", "other"),
        Q(owner=1),
        {"company": _flat(), "other": FanOut(Zipf()), "owner": _paired()},
        people=30,
    ),
    # Loads 12 of 20.
    _Case(
        "paired_key_null_relation_together",
        ("company",),
        Q(owner=1),
        {"company": _flat(null=0.3), "owner": _paired()},
        people=30,
        together=True,
    ),
    # Loads 13 of 20.
    _Case(
        "paired_pair_null_relation_together",
        ("company", "owner"),
        Q(owner=1),
        {"company": _flat(null=0.3), "owner": _paired()},
        people=30,
        together=True,
    ),
    # Loads 0 of 20.
    _Case(
        "paired_pair_null_condition_together",
        ("company", "owner"),
        Q(company=None),
        {"company": _flat(null=0.3), "owner": _paired()},
        people=30,
        together=True,
    ),
    # What follows has no fan-out and no tree in the constraint at all, so nothing used to
    # read it, whatever the condition named.
    # Loads 0 of 20.
    _Case(
        "fan_out_key_under_a_draw",
        ("label",),
        Q(owner=1),
        {"owner": FanOut(Zipf()), "label": _skew()},
    ),
    # Loads 15 of 20.
    _Case(
        "key_under_a_draw_at_twenty_rows",
        ("label",),
        Q(owner=1),
        {"owner": FanOut(Zipf()), "label": _skew()},
        rows=20,
    ),
    # Loads 18 of 20.
    _Case(
        "key_with_room_for_a_cap_under_a_draw",
        ("label",),
        Q(owner=1),
        {"owner": FanOut(Zipf()), "label": _skew()},
        rows=60,
        people=60,
    ),
    # Loads 0 of 20.
    _Case(
        "key_under_one_value",
        ("label",),
        Q(owner=1),
        {"owner": FanOut(Zipf()), "label": Constant("a")},
    ),
    # Loads 11 of 20.
    _Case(
        "tree_key_under_a_draw",
        ("label",),
        Q(prev=1),
        {"prev": _tree(), "label": _skew()},
    ),
    # Loads 12 of 20.
    _Case(
        "paired_key_under_a_draw",
        ("label",),
        Q(owner=1),
        {"company": _flat(), "owner": _paired(), "label": _skew()},
        people=30,
    ),
    # Loads 1 of 20.
    _Case(
        "paired_key_alone",
        ("owner",),
        Q(owner=1),
        {"company": _flat(), "owner": _paired()},
        people=30,
    ),
    # Loads 0 of 20.
    _Case(
        "paired_set_alone",
        ("owner",),
        Q(owner__in=[1, 2, 3]),
        {"company": _flat(), "owner": _paired()},
        people=30,
    ),
    # Loads 2 of 20.
    _Case(
        "paired_key_beside_a_draw",
        ("owner", "label"),
        Q(owner=1),
        {"company": _flat(), "owner": _paired(), "label": _skew()},
        people=30,
    ),
    # Loads 0 of 20.
    _Case(
        "null_share_under_a_draw",
        ("label",),
        Q(owner=None),
        {"owner": FanOut(Zipf(), null=0.5), "label": _skew()},
    ),
    # Loads 0 of 20.
    _Case(
        "roots_under_a_draw",
        ("label",),
        Q(prev=None),
        {"prev": _tree(), "label": _skew()},
    ),
    # Loads 0 of 20.
    _Case(
        "set_under_a_draw",
        ("label",),
        Q(owner__in=[1, 2]),
        {"owner": FanOut(Zipf()), "label": _skew()},
    ),
    # Loads 17 of 20.
    _Case(
        "set_under_a_cap_under_a_draw",
        ("label",),
        Q(owner__in=[1, 2]),
        {"owner": FanOut(Zipf(), max_children=1), "label": _skew()},
        rows=9,
    ),
    # Loads 0 of 20.
    _Case(
        "left_out_group_together",
        ("label",),
        Q(owner=1),
        {"owner": FanOut(Zipf())},
        together=True,
    ),
    # A pairing in the constraint keeps one key of its own relation and nothing else.
    # Two keys are two groups, whose partners were never kept apart.
    # Loads 2 of 20.
    _Case(
        "relation_set_over_its_pairing",
        ("owner",),
        Q(company__in=[1, 2, 3]),
        {"company": FanOut(Zipf()), "owner": _paired()},
        rows=60,
        companies=10,
        people=100,
    ),
    # The rows a null share leaves had no group to keep their partners apart in.
    # Loads 0 of 20.
    _Case(
        "relation_null_over_its_pairing",
        ("owner",),
        Q(company=None),
        {"company": FanOut(Zipf(), null=0.5), "owner": _paired()},
        rows=60,
        companies=10,
        people=100,
    ),
    # Loads 0 of 20.
    _Case(
        "relation_null_over_its_pairing_together",
        ("owner",),
        Q(company=None),
        {"company": FanOut(Zipf(), null=0.5), "owner": _paired()},
        rows=60,
        companies=10,
        people=100,
        together=True,
    ),
    # Loads 19 of 20.
    _Case(
        "another_key_over_a_pairing",
        ("owner",),
        Q(other=1),
        {"company": _flat(), "owner": _paired(), "other": FanOut(Zipf())},
        people=30,
    ),
    # Loads 17 of 20.
    _Case(
        "tree_key_over_a_pairing",
        ("owner",),
        Q(prev=1),
        {"company": _flat(), "owner": _paired(), "prev": _tree()},
        people=30,
    ),
    # Loads 19 of 20: room moves the odds and proves nothing.
    _Case(
        "key_over_many_parents_under_a_draw",
        ("label",),
        Q(owner=1),
        {"owner": FanOut(Zipf()), "label": _skew()},
        people=300,
    ),
    # Each of these loads 20 of 20 and is refused. A wide draw is not Distinct, a key is
    # read as able to land inside until the build reads the parent, and a pairing's relation
    # with one parent leaves one group, which nothing here counts.
    _Case(
        "paired_key_alone_over_one_parent",
        ("owner",),
        Q(owner=1),
        {"company": FanOut(Zipf(), parents=(1,)), "owner": _paired()},
        people=300,
    ),
    _Case(
        "key_under_a_wide_draw",
        ("label",),
        Q(owner=1),
        {"owner": FanOut(Zipf()), "label": Uniform(1, 10**9)},
    ),
    _Case(
        "absent_key_under_a_draw",
        ("label",),
        Q(owner=99),
        {"owner": FanOut(Zipf()), "label": _skew()},
        people=5,
    ),
    _Case(
        "absent_tree_key_under_a_draw",
        ("label",),
        Q(prev=500),
        {"prev": _tree(), "label": _skew()},
    ),
)


@pytest.mark.parametrize("case", _params(_REFUSED))
def test_a_relation_that_can_land_inside_the_condition_is_refused(case: _Case) -> None:
    message = _refusal(case)

    assert f"uq_{case.name}"[:60] in message
    assert "can write" in message
    assert "matter of the seed" in message


def _refused(name: str) -> _Case:
    return next(case for case in _REFUSED if case.name == name)


# Each of these loads 20 of 20.
_KEPT = (
    # Nothing can land inside, and each sits beside a refused table of the same size.
    _Case(
        "key_outside_parents",
        ("company",),
        Q(owner=1),
        {"company": FanOut(Zipf()), "owner": FanOut(Zipf(), parents=(2, 3))},
    ),
    _Case(
        "null_without_a_share",
        ("company",),
        Q(owner=None),
        {"company": FanOut(Zipf()), "owner": FanOut(Zipf())},
    ),
    _Case(
        "left_out_key",
        ("company",),
        Q(owner=1),
        {"company": FanOut(Zipf())},
    ),
    # At most one row is inside, whatever fills the rest. Three companies, so that a
    # second row would be likely to share a group.
    _Case(
        "cap_of_one",
        ("company",),
        Q(owner=1),
        {"company": FanOut(Zipf()), "owner": FanOut(Zipf(), max_children=1)},
        rows=60,
        companies=3,
        people=60,
    ),
    _Case(
        "flat_sizes",
        ("company",),
        Q(owner=1),
        {"company": FanOut(Zipf()), "owner": _flat()},
        rows=60,
        companies=3,
        people=60,
    ),
    _Case(
        "cap_of_one_with_a_null_share",
        ("company",),
        Q(owner=1),
        {"company": FanOut(Zipf()), "owner": FanOut(Zipf(), max_children=1, null=0.3)},
        rows=60,
        companies=3,
        people=60,
    ),
    _Case(
        "cap_of_one_with_nulls_together",
        ("company",),
        Q(owner=1),
        {"company": FanOut(Zipf()), "owner": FanOut(Zipf(), max_children=1, null=0.3)},
        rows=60,
        companies=3,
        people=60,
        together=True,
    ),
    _Case(
        "flat_tree_key",
        ("company",),
        Q(prev=1),
        {"company": FanOut(Zipf()), "prev": _flat_tree()},
        companies=3,
    ),
    _Case(
        "one_root",
        ("company",),
        Q(prev=None),
        {"company": FanOut(Zipf()), "prev": Tree(roots=1 / 200, sizes=Zipf())},
        companies=3,
    ),
    _Case(
        "one_root_grouped_together",
        ("prev",),
        Q(prev=None),
        {"prev": Tree(roots=1 / 200, sizes=Zipf())},
        together=True,
    ),
    # A NULL in a column of the constraint keeps its row out of the index.
    _Case(
        "own_null_share",
        ("owner",),
        Q(owner=None),
        {"owner": FanOut(Zipf(), null=0.3)},
    ),
    _Case(
        "own_null_by_its_attname",
        ("owner",),
        Q(owner_id=None),
        {"owner": FanOut(Zipf(), null=0.3)},
    ),
    _Case(
        "own_null_beside_a_draw",
        ("owner", "label"),
        Q(owner=None),
        {"owner": FanOut(Zipf(), null=0.3), "label": _skew()},
    ),
    _Case(
        "own_roots",
        ("prev",),
        Q(prev=None),
        {"prev": _tree()},
    ),
    # A Paired against its own relation.
    _Case(
        "paired_key_over_its_relation",
        ("company",),
        Q(owner=1),
        {"company": _flat(), "owner": _paired()},
        people=30,
    ),
    _Case(
        "paired_key_beside_a_draw",
        ("company", "label"),
        Q(owner=1),
        {"company": _flat(), "owner": _paired(), "label": _skew()},
        people=30,
    ),
    _Case(
        "paired_key_null_relation_apart",
        ("company",),
        Q(owner=1),
        {"company": _flat(null=0.3), "owner": _paired()},
        people=30,
    ),
    _Case(
        "paired_key_no_null_share_together",
        ("company",),
        Q(owner=1),
        {"company": _flat(), "owner": _paired()},
        people=30,
        together=True,
    ),
    _Case(
        "paired_pair_no_null_share_together",
        ("company", "owner"),
        Q(company=1),
        {"company": _flat(), "owner": _paired()},
        people=30,
        together=True,
    ),
    _Case(
        "paired_pair_key",
        ("company", "owner"),
        Q(owner=1),
        {"company": _flat(), "owner": _paired()},
        people=30,
    ),
    _Case(
        "paired_pair_relation_key",
        ("company", "owner"),
        Q(company=1),
        {"company": _flat(), "owner": _paired()},
        people=30,
    ),
    _Case(
        "paired_pair_null_relation_apart",
        ("company", "owner"),
        Q(owner=1),
        {"company": _flat(null=0.3), "owner": _paired()},
        people=30,
    ),
    _Case(
        "paired_pair_null_condition_apart",
        ("company", "owner"),
        Q(company=None),
        {"company": _flat(null=0.3), "owner": _paired()},
        people=30,
    ),
    _Case(
        "paired_pair_under_a_draw",
        ("company", "owner"),
        Q(label="a"),
        {"company": _flat(), "owner": _paired(), "label": _skew()},
        people=30,
    ),
    _Case(
        "paired_outside_parents",
        ("other",),
        Q(owner=1),
        {
            "company": _flat(),
            "other": FanOut(Zipf()),
            "owner": _paired(parents=tuple(range(2, 31))),
        },
        people=30,
    ),
    _Case(
        "paired_null",
        ("other",),
        Q(owner=None),
        {"company": _flat(), "other": FanOut(Zipf()), "owner": _paired()},
        people=30,
    ),
    _Case(
        "paired_null_beside_a_null_relation",
        ("other",),
        Q(owner=None),
        {"company": _flat(null=0.3), "other": FanOut(Zipf()), "owner": _paired()},
        people=30,
    ),
    # A column beside the relation settles it, or may: left to the other two nets.
    _Case(
        "distinct_beside",
        ("company", "label"),
        Q(owner=1),
        {"company": FanOut(Zipf()), "owner": FanOut(Zipf()), "label": Sequential(1, 1)},
    ),
    _Case(
        "derived_beside",
        ("company", "label"),
        Q(owner=1),
        {
            "company": FanOut(Zipf()),
            "owner": FanOut(Zipf()),
            "label": Derived("company", compute=_named_for_position, scope="group"),
        },
    ),
    # One key of the relation a pairing takes its groups from is one group, and partners
    # are distinct inside a group, so every row inside holds a different partner. Four
    # rows of every company over four people is the tightest that builds.
    _Case(
        "relation_key_over_its_pairing",
        ("owner",),
        Q(company=1),
        {"company": _flat(), "owner": _paired()},
        people=4,
    ),
    _Case(
        "relation_key_over_a_busy_group_of_its_pairing",
        ("owner",),
        Q(company=1),
        {"company": FanOut(Zipf()), "owner": _paired()},
        rows=60,
        companies=10,
        people=60,
    ),
    _Case(
        "relation_key_over_its_pairing_beside_a_draw",
        ("owner", "label"),
        Q(company=1),
        {"company": _flat(), "owner": _paired(), "label": _skew()},
        people=4,
    ),
    _Case(
        "relation_key_over_its_pairing_beside_a_fan_out",
        ("owner", "other"),
        Q(company=1),
        {"company": _flat(), "owner": _paired(), "other": FanOut(Zipf())},
        people=4,
    ),
    # A key holds none of the NULLs, however they are counted.
    _Case(
        "relation_key_over_its_pairing_with_a_null_share",
        ("owner",),
        Q(company=1),
        {"company": _flat(null=0.3), "owner": _paired()},
        people=30,
    ),
    _Case(
        "relation_key_over_its_pairing_together",
        ("owner",),
        Q(company=1),
        {"company": _flat(null=0.3), "owner": _paired()},
        people=30,
        together=True,
    ),
    # Each label left out is a NULL of its own, so no row reaches the index.
    _Case(
        "key_under_a_left_out_group",
        ("label",),
        Q(owner=1),
        {"owner": FanOut(Zipf())},
    ),
)


@_ON_POSTGRES
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("case", _params(_KEPT))
def test_a_relation_that_cannot_break_the_condition_is_accepted_and_loads(case: _Case) -> None:
    _assert_loads(case)


_REMEDIES = (
    pytest.param(
        _refused("key_over_a_fan_out"),
        ("parents= naming only keys outside the condition", "left out of the shape"),
        ("max_children=1", "PerParent(", "Distinct", "no null share"),
        (
            _refused("key_over_a_fan_out")._replace(
                name="remedy_narrowed",
                columns={"company": FanOut(Zipf()), "owner": FanOut(Zipf(), parents=(2, 3))},
            ),
            _refused("key_over_a_fan_out")._replace(
                name="remedy_left_out", columns={"company": FanOut(Zipf())}
            ),
        ),
        id="key_over_a_fan_out_with_more_rows_than_parents",
    ),
    pytest.param(
        _Case(
            "key_with_room_for_a_cap",
            ("company",),
            Q(owner=1),
            {"company": FanOut(Zipf()), "owner": FanOut(Zipf())},
            rows=60,
            companies=3,
            people=60,
        ),
        ("max_children=1",),
        (),
        (
            _Case(
                "remedy_capped",
                ("company",),
                Q(owner=1),
                {"company": FanOut(Zipf()), "owner": FanOut(Zipf(), max_children=1)},
                rows=60,
                companies=3,
                people=60,
            ),
        ),
        id="key_with_a_parent_for_every_row",
    ),
    pytest.param(
        _refused("null_share"),
        ("no null share",),
        ("parents=", "max_children=1", "left out of the shape", "Distinct"),
        (
            _refused("null_share")._replace(
                name="remedy_no_null",
                columns={"company": FanOut(Zipf()), "owner": FanOut(Zipf())},
            ),
        ),
        id="null_share",
    ),
    pytest.param(
        _Case(
            "key_or_null_under_company",
            ("company",),
            Q(owner=1) | Q(owner=None),
            {"company": FanOut(Zipf()), "owner": FanOut(Zipf(), null=0.3)},
        ),
        ("parents= naming only keys outside the condition and no null share",),
        ("left out of the shape", "max_children=1"),
        (
            _Case(
                "remedy_narrowed_no_null",
                ("company",),
                Q(owner=1) | Q(owner=None),
                {"company": FanOut(Zipf()), "owner": FanOut(Zipf(), parents=(2, 3))},
            ),
        ),
        id="key_and_null_share",
    ),
    pytest.param(
        _refused("key_over_a_tree"),
        ("sizes that hold one value", "left out of the shape"),
        ("parents=", "max_children=1", "PerParent(", "Distinct"),
        (
            _refused("key_over_a_tree")._replace(
                name="remedy_flat_tree",
                columns={"company": FanOut(Zipf()), "prev": _flat_tree()},
            ),
            _refused("key_over_a_tree")._replace(
                name="remedy_tree_out", columns={"company": FanOut(Zipf())}
            ),
        ),
        id="key_over_a_tree",
    ),
    pytest.param(
        _refused("roots_under_a_fan_out"),
        ("one root",),
        ("sizes that hold one value", "left out of the shape", "parents="),
        (
            _refused("roots_under_a_fan_out")._replace(
                name="remedy_one_root",
                columns={"company": FanOut(Zipf()), "prev": Tree(roots=1 / 200, sizes=Zipf())},
            ),
        ),
        id="roots_under_a_fan_out",
    ),
    pytest.param(
        _refused("own_fan_out_beside_a_draw"),
        ("Distinct",),
        (),
        (
            _refused("own_fan_out_beside_a_draw")._replace(
                name="remedy_distinct",
                columns={"company": FanOut(Zipf()), "label": Sequential(1, 1)},
            ),
        ),
        id="own_fan_out_beside_a_draw",
    ),
    pytest.param(
        _refused("paired_set_over_its_relation"),
        ("left out of the shape",),
        ("parents=", "max_children=1", "Distinct"),
        (
            _refused("paired_set_over_its_relation")._replace(
                name="remedy_pairing_out", columns={"company": _flat()}
            ),
        ),
        id="paired_offers_neither_a_narrowing_nor_a_cap",
    ),
    pytest.param(
        _refused("fan_out_key_under_a_draw"),
        ("parents= naming only keys outside the condition", "left out of the shape", "Distinct"),
        ("max_children=1", "no null share", "PerParent("),
        (
            _refused("fan_out_key_under_a_draw")._replace(
                name="draw_remedy_narrowed",
                columns={"owner": FanOut(Zipf(), parents=(2, 3)), "label": _skew()},
            ),
            _refused("fan_out_key_under_a_draw")._replace(
                name="draw_remedy_left_out", columns={"label": _skew()}
            ),
            _refused("fan_out_key_under_a_draw")._replace(
                name="draw_remedy_distinct",
                columns={"owner": FanOut(Zipf()), "label": Sequential(1, 1)},
            ),
        ),
        id="key_under_a_draw",
    ),
    pytest.param(
        _refused("key_with_room_for_a_cap_under_a_draw"),
        ("max_children=1",),
        (),
        (
            _refused("key_with_room_for_a_cap_under_a_draw")._replace(
                name="draw_remedy_capped",
                columns={"owner": FanOut(Zipf(), max_children=1), "label": _skew()},
            ),
        ),
        id="key_under_a_draw_with_a_parent_for_every_row",
    ),
    pytest.param(
        _refused("null_share_under_a_draw"),
        ("no null share", "Distinct"),
        ("parents=", "max_children=1", "left out of the shape"),
        (
            _refused("null_share_under_a_draw")._replace(
                name="draw_remedy_no_null",
                columns={"owner": FanOut(Zipf()), "label": _skew()},
            ),
            _refused("null_share_under_a_draw")._replace(
                name="draw_remedy_distinct_over_nulls",
                columns={"owner": FanOut(Zipf(), null=0.5), "label": Sequential(1, 1)},
            ),
        ),
        id="null_share_under_a_draw",
    ),
    pytest.param(
        _refused("tree_key_under_a_draw"),
        ("sizes that hold one value", "left out of the shape", "Distinct"),
        ("parents=", "max_children=1"),
        (
            _refused("tree_key_under_a_draw")._replace(
                name="draw_remedy_flat_tree",
                columns={"prev": _flat_tree(), "label": _skew()},
            ),
            _refused("tree_key_under_a_draw")._replace(
                name="draw_remedy_no_tree", columns={"label": _skew()}
            ),
            _refused("tree_key_under_a_draw")._replace(
                name="draw_remedy_distinct_beside_a_tree",
                columns={"prev": _tree(), "label": Sequential(1, 1)},
            ),
        ),
        id="tree_key_under_a_draw",
    ),
    pytest.param(
        _refused("roots_under_a_draw"),
        ("roots=1/200", "Distinct"),
        ("left out of the shape", "sizes that hold one value"),
        (
            _refused("roots_under_a_draw")._replace(
                name="draw_remedy_one_root",
                columns={"prev": Tree(roots=1 / 200, sizes=Zipf()), "label": _skew()},
            ),
            _refused("roots_under_a_draw")._replace(
                name="draw_remedy_distinct_beside_roots",
                columns={"prev": _tree(), "label": Sequential(1, 1)},
            ),
        ),
        id="roots_under_a_draw",
    ),
    pytest.param(
        _refused("paired_key_under_a_draw"),
        ("left out of the shape", "Distinct"),
        ("parents=", "max_children=1"),
        (
            _refused("paired_key_under_a_draw")._replace(
                name="draw_remedy_no_pairing",
                columns={"company": _flat(), "label": _skew()},
            ),
            _refused("paired_key_under_a_draw")._replace(
                name="draw_remedy_distinct_beside_a_pairing",
                columns={"company": _flat(), "owner": _paired(), "label": Sequential(1, 1)},
            ),
        ),
        id="paired_key_under_a_draw",
    ),
    pytest.param(
        _refused("paired_key_alone"),
        ("left out of the shape",),
        ("Distinct", "parents=", "max_children=1"),
        (
            _refused("paired_key_alone")._replace(
                name="alone_remedy_no_pairing", columns={"company": _flat()}
            ),
        ),
        id="paired_key_alone",
    ),
    pytest.param(
        _refused("left_out_group_together"),
        ("parents= naming only keys outside the condition", "left out of the shape", "Distinct"),
        ("max_children=1",),
        (
            _refused("left_out_group_together")._replace(
                name="tied_remedy_narrowed",
                columns={"owner": FanOut(Zipf(), parents=(2, 3))},
            ),
            _refused("left_out_group_together")._replace(name="tied_remedy_left_out", columns={}),
            _refused("left_out_group_together")._replace(
                name="tied_remedy_distinct",
                columns={"owner": FanOut(Zipf()), "label": Sequential(1, 1)},
            ),
        ),
        id="left_out_group_together",
        marks=_NEEDS_DJANGO_5,
    ),
    pytest.param(
        _refused("relation_set_over_its_pairing"),
        ("parents= naming only keys outside the condition",),
        ("left out of the shape", "Distinct", "max_children=1"),
        (
            _refused("relation_set_over_its_pairing")._replace(
                name="pairing_remedy_narrowed",
                columns={"company": FanOut(Zipf(), parents=(4, 5, 6)), "owner": _paired()},
            ),
        ),
        id="relation_set_over_its_pairing",
    ),
    pytest.param(
        _refused("relation_null_over_its_pairing"),
        ("no null share",),
        ("left out of the shape", "Distinct", "parents="),
        (
            _refused("relation_null_over_its_pairing")._replace(
                name="pairing_remedy_no_null",
                columns={"company": FanOut(Zipf()), "owner": _paired()},
            ),
        ),
        id="relation_null_over_its_pairing",
    ),
    pytest.param(
        _refused("another_key_over_a_pairing"),
        ("parents= naming only keys outside the condition", "left out of the shape"),
        ("Distinct", "max_children=1"),
        (
            _refused("another_key_over_a_pairing")._replace(
                name="pairing_remedy_other_narrowed",
                columns={
                    "company": _flat(),
                    "owner": _paired(),
                    "other": FanOut(Zipf(), parents=(2, 3)),
                },
            ),
            _refused("another_key_over_a_pairing")._replace(
                name="pairing_remedy_other_left_out",
                columns={"company": _flat(), "owner": _paired()},
            ),
        ),
        id="another_key_over_a_pairing",
    ),
    pytest.param(
        _refused("tree_key_over_a_pairing"),
        ("sizes that hold one value", "left out of the shape"),
        ("Distinct", "parents="),
        (
            _refused("tree_key_over_a_pairing")._replace(
                name="pairing_remedy_flat_tree",
                columns={"company": _flat(), "owner": _paired(), "prev": _flat_tree()},
            ),
            _refused("tree_key_over_a_pairing")._replace(
                name="pairing_remedy_no_tree",
                columns={"company": _flat(), "owner": _paired()},
            ),
        ),
        id="tree_key_over_a_pairing",
    ),
)


@_ON_POSTGRES
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("refused", "offered", "withheld", "builds"), _REMEDIES)
def test_the_refusal_offers_only_what_builds(
    refused: _Case,
    offered: tuple[str, ...],
    withheld: tuple[str, ...],
    builds: tuple[_Case, ...],
) -> None:
    message = _refusal(refused)

    assert "Projection" in message
    for one in offered:
        assert one in message
    for one in withheld:
        assert one not in message
    for one in builds:
        _assert_loads(one)


@_ON_POSTGRES
@pytest.mark.django_db(transaction=True)
def test_the_statement_the_refusal_offers_builds() -> None:
    model = _model(_refused("key_over_a_fan_out")._replace(name="remedy_statement"))
    quote = connection.ops.quote_name
    with connection.schema_editor() as editor:
        editor.create_model(model)
    try:
        build_shape(
            Shape(
                Table(Company, rows=5, name=Constant("acme")),
                Projection(
                    model,
                    columns=("id", "company"),
                    sql=f"SELECT id, id FROM {quote(Company._meta.db_table)}",
                ),
            )
        )
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {quote(model._meta.db_table)}")
            assert cursor.fetchone()[0] == 5
    finally:
        with connection.schema_editor() as editor:
            editor.delete_model(model)


@_ON_POSTGRES
@pytest.mark.django_db(transaction=True)
def test_the_statement_the_refusal_offers_builds_under_a_draw() -> None:
    model = _model(_refused("fan_out_key_under_a_draw")._replace(name="draw_remedy_statement"))
    quote = connection.ops.quote_name
    with connection.schema_editor() as editor:
        editor.create_model(model)
    try:
        build_shape(
            Shape(
                Table(Company, rows=5, name=Constant("acme")),
                Projection(
                    model,
                    columns=("id", "label"),
                    sql=f"SELECT id, 'at-' || id FROM {quote(Company._meta.db_table)}",
                ),
            )
        )
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {quote(model._meta.db_table)}")
            assert cursor.fetchone()[0] == 5
    finally:
        with connection.schema_editor() as editor:
            editor.delete_model(model)


# No relation fills the condition and nothing partitions the constraint, or a derivation
# stands in the group. Each is accepted and fails in COPY, and docs/invariants.md names it.
_LEFT_TO_THE_NETS = (
    # Loads 0 of 20.
    _Case(
        "drawn_condition_over_a_draw",
        ("label",),
        Q(state="x"),
        {"state": Skew({"x": 0.1, "y": 0.9}), "label": _skew()},
    ),
    # Loads 10 of 20.
    _Case(
        "rare_condition_over_a_draw",
        ("label",),
        Q(state="x"),
        {"state": Skew({"x": 0.01, "y": 0.99}), "label": _skew()},
    ),
    # Loads 0 of 20.
    _Case(
        "constant_condition_over_a_draw",
        ("label",),
        Q(state="x"),
        {"state": Constant("x"), "label": _skew()},
    ),
    # Loads 0 of 20.
    _Case(
        "condition_on_the_draw_itself",
        ("label",),
        Q(label="a"),
        {"label": _skew()},
    ),
    # Loads 0 of 20.
    _Case(
        "drawn_condition_over_a_pairing",
        ("owner",),
        Q(state="x"),
        {"company": _flat(), "owner": _paired(), "state": Skew({"x": 0.1, "y": 0.9})},
        people=30,
    ),
    # Loads 0 of 20.
    _Case(
        "rule_per_parent_over_a_draw",
        ("label",),
        Q(state="x"),
        {
            "company": FanOut(Zipf()),
            "state": PerParent("company", last="x", rest="y"),
            "label": _skew(),
        },
    ),
    # Loads 0 of 20.
    _Case(
        "rule_per_parent_over_a_pairing",
        ("owner",),
        Q(state="x"),
        {
            "company": _flat(),
            "owner": _paired(),
            "state": PerParent("company", last="x", rest="y"),
        },
        people=30,
    ),
    # Loads 0 of 20.
    _Case(
        "key_a_db_default_holds_under_a_draw",
        ("label",),
        Q(owner=1),
        {"label": _skew()},
        owner_db_default=1,
    ),
    # Loads 1 of 20.
    _Case(
        "key_under_a_derivation",
        ("label",),
        Q(owner=1),
        {
            "owner": FanOut(Zipf()),
            "company": FanOut(Zipf()),
            "label": Derived("company", compute=_named_for_position, scope="group"),
        },
    ),
)


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            case,
            id=case.name,
            marks=[_NEEDS_DJANGO_5] if case.owner_db_default is not None else [],
        )
        for case in _LEFT_TO_THE_NETS
    ],
)
def test_what_no_relation_decides_over_draws_is_left_to_the_other_two_nets(case: _Case) -> None:
    Shape(*_tables(case, _model(case)))


def test_the_tree_remedies_are_not_offered_where_they_prove_nothing() -> None:
    # Two children of two rows are two rows inside, so sizes of one value do not keep it,
    # and one root leaves a key with every child there is.
    assert "sizes that hold one value" not in _refusal(_refused("set_over_a_flat_tree"))
    assert "sizes that hold one value" in _refusal(_refused("key_over_one_root"))
    assert "Tree(roots=1/200" not in _refusal(_refused("key_over_one_root"))
    assert "Tree(roots=1/200" in _refusal(_refused("roots_of_a_flat_tree"))


def test_the_cap_is_not_offered_for_a_set_or_for_a_null() -> None:
    wide = {"company": FanOut(Zipf()), "owner": FanOut(Zipf())}
    two_keys = _Case("set_with_room_for_a_cap", ("company",), Q(owner__in=[1, 2]), wide, rows=60)
    nulls = _Case(
        "null_with_room_for_a_cap",
        ("company",),
        Q(owner=None),
        {"company": FanOut(Zipf()), "owner": FanOut(Zipf(), null=0.3)},
        rows=5,
    )

    assert "max_children=1" not in _refusal(two_keys._replace(people=60))
    assert "max_children=1" not in _refusal(nulls)


def test_a_cap_is_not_offered_where_the_parents_are_unknown() -> None:
    # A parent this shape does not build: the cap needs a parent for every row, and
    # nothing here can say there are enough.
    message = _refusal(
        _refused("key_over_a_fan_out")._replace(name="key_over_unknown_parents", people=None)
    )

    assert "max_children=1" not in message
    assert "parents= naming only keys outside the condition" in message


def test_narrowing_is_not_offered_where_no_other_key_exists() -> None:
    # One person, and the condition names them, so there is no key to narrow to.
    message = _refusal(
        _refused("key_over_a_fan_out")._replace(name="key_over_the_only_parent", people=1)
    )

    assert "parents=" not in message


def test_leaving_it_out_is_not_offered_where_the_column_cannot_be_null() -> None:
    message = _refusal(
        _refused("key_over_a_fan_out")._replace(
            name="key_on_a_required_column", owner_required=True
        )
    )

    assert "left out of the shape" not in message


@_NEEDS_DJANGO_5
def test_leaving_it_out_is_not_offered_where_a_db_default_would_fill_it() -> None:
    # Left out, the column holds its default in every row, and that key is the condition.
    message = _refusal(
        _refused("key_over_a_fan_out")._replace(
            name="key_on_a_defaulted_column", condition=Q(owner=2), owner_db_default=2
        )
    )

    assert "left out of the shape" not in message


@pytest.mark.parametrize(
    "reader",
    [
        pytest.param(_paired(), id="a_paired"),
        pytest.param(Derived("company", compute=_named_for_position, scope="group"), id="derived"),
    ],
)
def test_leaving_it_out_is_not_offered_where_another_column_reads_it(reader: Any) -> None:
    # Table refuses a Paired or a derivation over a relation the shape does not declare.
    column = "owner" if isinstance(reader, Paired) else "label"
    message = _refusal(
        _Case(
            f"key_read_by_{column}",
            ("company",),
            Q(company=1),
            {"company": FanOut(Zipf()), column: reader},
            people=30,
        )
    )

    assert "left out of the shape" not in message
