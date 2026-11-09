"""Reading a factory, and reporting what it does not vary.

The tests are about the **findings**, not the declaration. Inference is the easy
half and it is not what this is for: a factory measured faithfully produces the
flat world this package exists to argue against, so the value is in saying so.
"""

from __future__ import annotations

from collections.abc import Callable

import django
import pytest
from django.db import connection
from django.db.models import Model

import django_data_shape
from django_data_shape import InvalidShape, Shape, Table, Tree, build, shape_from_factory
from tests.testapp.models import (
    Category,
    Chain,
    Checked,
    Coded,
    Company,
    Exclusive,
    Folder,
    Mirror,
    Project,
    Relay,
    RootlessLink,
    RootlessNode,
    Session,
    Shelf,
    Sibling,
    SpelledChain,
    SpelledFolder,
    SpelledRelay,
    Successor,
    TiedLink,
)

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="it runs a factory against a database and rolls it back",
    ),
]


_NEEDS_NULLS_DISTINCT = pytest.mark.skipif(
    django.VERSION < (5, 0), reason="UniqueConstraint(nulls_distinct=) arrived in Django 5.0"
)


def _sub_factory() -> Session:
    """What ``company = SubFactory(CompanyFactory)`` does: a parent per child."""
    return Session.objects.create(company=Company.objects.create(name="acme"), label="s")


def _shared_parent(companies: list[Company]) -> Session:
    return Session.objects.create(company=companies[Session.objects.count() % 4], label="s")


def test_a_sub_factory_is_reported_as_a_fan_out_of_degree_one() -> None:
    """The finding worth running this for, and the one no source shows.

    A parent per child is a fan-out where every parent has exactly one row, so
    the average is the truth and the join estimate cannot miss. Nothing in the
    factory says so -- it is only visible in what the calls wrote.
    """
    report = shape_from_factory(_sub_factory, samples=25)

    assert "Company grew by exactly one per call -- a sub-factory" in report
    assert "cannot be misestimated" in report


def test_a_reused_parent_is_reported_with_its_real_ratio() -> None:
    companies = [Company.objects.create(name=f"c{i}") for i in range(4)]

    report = shape_from_factory(lambda: _shared_parent(companies), samples=20)

    # A round-robin is the other unrealistic way a factory reaches a foreign
    # key: not one parent per child, but the same number for every parent. The
    # average is the truth either way.
    assert "every one of the 4 parents got exactly 5 row(s)" in report
    assert "cannot miss" in report


def test_a_column_the_factory_fixes_is_a_finding_and_not_just_a_constant() -> None:
    # The whole point. Writing Constant('ACTIVE') down without comment would be
    # this package blessing the flat world it exists to expose.
    companies = [Company.objects.create(name=f"c{i}") for i in range(4)]

    report = shape_from_factory(lambda: _shared_parent(companies), samples=20)

    assert "Session.label" in report
    assert "The factory fixes this column" in report


def test_the_report_says_it_is_source_to_read_rather_than_a_shape() -> None:
    companies = [Company.objects.create(name=f"c{i}") for i in range(4)]

    report = shape_from_factory(lambda: _shared_parent(companies), samples=20)

    assert isinstance(report, str)
    assert "not a shape to" in report
    assert "Table(Session, rows=..." in report


def test_the_sample_size_is_stated_because_the_answer_moves_with_it() -> None:
    companies = [Company.objects.create(name=f"c{i}") for i in range(4)]

    assert "Read at 20 samples" in shape_from_factory(lambda: _shared_parent(companies), samples=20)


def test_nothing_the_factory_made_is_left_behind() -> None:
    """It runs against a real database, so it must not write to one."""
    shape_from_factory(_sub_factory, samples=10)

    assert Session.objects.count() == 0
    assert Company.objects.count() == 0


def test_one_sample_cannot_tell_a_constant_from_a_distribution() -> None:
    with pytest.raises(InvalidShape, match="at least two samples"):
        shape_from_factory(_sub_factory, samples=1)


def test_a_factory_that_returns_nothing_is_refused_by_name() -> None:
    with pytest.raises(InvalidShape, match="expects the factory to return a model instance"):
        shape_from_factory(lambda: None, samples=5)  # type: ignore[arg-type, return-value]


def test_a_column_with_a_value_per_row_is_named_as_such() -> None:
    companies = [Company.objects.create(name=f"c{i}") for i in range(4)]
    counter = {"n": 0}

    def factory() -> Project:
        counter["n"] += 1
        return Project.objects.create(
            company=companies[counter["n"] % 4],
            status=f"S{counter['n']}",
            created_at="2020-01-01T00:00:00Z",
        )

    report = shape_from_factory(factory, samples=8)

    assert "every value distinct" in report


def test_a_column_with_many_values_is_not_written_out_in_full() -> None:
    # A Skew listing sixty values is not a declaration a person keeps, so past a
    # point it says how many there were and what the head is instead.
    companies = [Company.objects.create(name=f"c{i}") for i in range(4)]
    counter = {"n": 0}

    def factory() -> Session:
        counter["n"] += 1
        return Session.objects.create(
            company=companies[counter["n"] % 4], label=f"L{counter['n'] % 30}"
        )

    report = shape_from_factory(factory, samples=60)

    assert "30 distinct values" in report
    assert "more than a Skew wants written out" in report


def test_every_row_pointing_at_one_parent_is_not_a_fan_out_at_all() -> None:
    company = Company.objects.create(name="only")

    report = shape_from_factory(
        lambda: Session.objects.create(company=company, label="s"), samples=20
    )

    assert "every row pointed at the same parent" in report
    assert "no distribution across the join" in report


def test_a_relation_that_is_actually_skewed_gets_no_finding() -> None:
    # The direction that must stay quiet. A factory doing the right thing should
    # not be told off for it, or the findings stop meaning anything.
    companies = [Company.objects.create(name=f"c{i}") for i in range(4)]
    counter = {"n": 0}

    def factory() -> Session:
        counter["n"] += 1
        pick = 0 if counter["n"] % 3 else counter["n"] % 4
        return Session.objects.create(company=companies[pick], label=f"L{counter['n'] % 3}")

    report = shape_from_factory(factory, samples=30)

    assert "at the head and" in report
    assert "cannot miss" not in report


def test_a_parent_created_less_than_once_per_call_reports_its_ratio() -> None:
    counter = {"n": 0}

    def factory() -> Session:
        counter["n"] += 1
        if counter["n"] % 5 == 1:
            Company.objects.create(name=f"c{counter['n']}")
        return Session.objects.create(company=Company.objects.first(), label="s")

    report = shape_from_factory(factory, samples=20)

    assert "Company grew by 4 over 20 calls, about 5.0 children per parent" in report


def test_a_factory_that_varies_everything_gets_a_declaration_and_no_findings() -> None:
    """The case the tool is not for, and it has to stay quiet about it."""
    companies = [Company.objects.create(name=f"c{i}") for i in range(4)]
    counter = {"n": 0}

    def factory() -> Session:
        counter["n"] += 1
        pick = 0 if counter["n"] % 3 else counter["n"] % 4
        return Session.objects.create(company=companies[pick], label=f"L{counter['n'] % 3}")

    report = shape_from_factory(factory, samples=30)

    assert "What your factory does not vary" not in report
    assert "Table(Session, rows=..." in report


def test_a_factory_needing_arguments_is_told_so_rather_than_failing_in_the_database() -> None:
    """A mature codebase's factories take arguments, and most need them.

    Called with none, they leave a required column empty and the failure arrives
    as a raw `IntegrityError` naming a constraint -- which says nothing about
    what the caller did or what to do instead, and is the one thing every other
    refusal in this package avoids.
    """

    def needs_a_parent(company=None) -> Session:
        return Session.objects.create(company=company, label="s")

    with pytest.raises(InvalidShape) as raised:
        shape_from_factory(needs_a_parent, samples=5)

    message = str(raised.value)
    assert "could not be called" in message
    assert "defaults=" in message
    # The driver's own words are quoted here on purpose: unlike a refusal about
    # a declaration, the caller needs to see which column their factory left
    # empty, and nothing here retains a value this package generated.
    assert "IntegrityError" in message


def test_defaults_are_passed_to_every_call() -> None:
    company = Company.objects.create(name="acme")

    def needs_a_parent(company=None) -> Session:
        return Session.objects.create(company=company, label="s")

    report = shape_from_factory(needs_a_parent, samples=6, defaults={"company": company})

    assert "Table(Session, rows=..." in report
    assert "every row pointed at the same parent" in report


def test_a_factory_that_fails_for_its_own_reasons_says_which_call_it_was() -> None:
    # Not every failure is a missing argument. A factory that works and then
    # stops -- a unique constraint met on the fourth call, say -- needs the call
    # number, because "it failed" and "it failed after three" are different bugs.
    calls = {"n": 0}

    def eventually_fails() -> Session:
        calls["n"] += 1
        if calls["n"] > 3:
            raise RuntimeError("no more room")
        return Session.objects.create(company=Company.objects.create(name="c"), label="s")

    with pytest.raises(InvalidShape) as raised:
        shape_from_factory(eventually_fails, samples=10)

    assert "call 4 of 10" in str(raised.value)


def _built(report: str, model: type[Model], rows: int = 100) -> Table:
    """Build the emitted source against ``Table``, which is the whole claim.

    The report is source with ``rows=...`` left for the reader to fill, so the
    only edit made is the one a reader would: a row count. Everything else is
    evaluated as written, so a declaration ``Table`` refuses -- the defect this
    guards -- raises here rather than passing as a string that merely looks right.
    """
    names = {**vars(django_data_shape), model.__name__: model}
    table = eval(report.replace("rows=...", f"rows={rows}"), names)  # noqa: S307
    assert isinstance(table, Table)
    return table


def _tree_factory(model: type[Model], parents: Callable[[int], int | None]) -> Callable[[], Model]:
    """A factory over a self-keyed model; ``parents(n)`` is the nth call's parent index.

    The index counts calls, 1-based, and ``None`` makes a root. A parent is
    always an earlier call's row, so the factory could be a real one.
    """
    made: list[Model] = []

    def factory() -> Model:
        n = len(made) + 1
        index = parents(n)
        row = model.objects.create(  # type: ignore[attr-defined]
            name=f"n{n % 3}", parent=None if index is None else made[index]
        )
        made.append(row)
        return row

    return factory


def test_a_self_referential_key_is_a_tree_that_table_accepts() -> None:
    """The defect: the report said ``FanOut``, which ``Table`` refuses on a self-key."""
    factory = _tree_factory(Category, lambda n: None if n % 4 == 1 else (n * 7) % (n - 1))

    report = shape_from_factory(factory, samples=20)

    assert "parent=Tree(roots=0.25, sizes=Zipf())" in report
    declared = _built(report, Category).fields["parent"]
    assert isinstance(declared, Tree)
    assert declared.roots == 0.25
    assert "FanOut" not in report


def test_the_measured_root_share_counts_the_nulls_and_not_a_parent_called_none() -> None:
    # Every child points at the one root, so a Counter over the values would see
    # two "parents" -- the root's key and None -- and call this a flat fan-out.
    factory = _tree_factory(Category, lambda n: None if n == 1 else 0)

    report = shape_from_factory(factory, samples=40)

    assert "parent=Tree(roots=0.025, sizes=Zipf())" in report
    assert isinstance(_built(report, Category).fields["parent"], Tree)


def test_a_self_key_nobody_left_null_is_the_figure_table_offers_and_a_finding() -> None:
    # A root made outside the calls, so every measured row has a parent: a root
    # share of zero, which Tree refuses, so the figure Table's own refusal
    # offers is written instead and the finding says it is not a measurement.
    root = Category.objects.create(name="root")

    report = shape_from_factory(lambda: Category.objects.create(name="x", parent=root), samples=20)

    assert "parent=Tree(roots=0.05, sizes=Zipf())" in report
    assert "Category.parent: no row was left without a parent" in report
    assert "not measured" in report
    assert isinstance(_built(report, Category).fields["parent"], Tree)


def test_a_self_key_always_left_null_is_the_figure_table_offers_and_a_finding() -> None:
    report = shape_from_factory(lambda: Category.objects.create(name="x"), samples=20)

    assert "parent=Tree(roots=0.05, sizes=Zipf())" in report
    assert "Category.parent: every row was left without a parent" in report
    assert "not measured" in report
    assert isinstance(_built(report, Category).fields["parent"], Tree)


def test_a_root_share_that_rounds_to_one_is_written_in_full() -> None:
    # 2499 roots in 2500 calls is a share of 0.9996, which three figures write
    # as 1 -- and Tree refuses a share of 1. So it is written as it was measured.
    first = Category.objects.create(name="root")
    calls = {"n": 0}

    def factory() -> Category:
        calls["n"] += 1
        return Category.objects.create(name="x", parent=first if calls["n"] == 1 else None)

    report = shape_from_factory(factory, samples=2500)

    assert "parent=Tree(roots=0.9996, sizes=Zipf())" in report
    assert _built(report, Category).fields["parent"].roots == 0.9996  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("model", "why"),
    [
        (RootlessNode, "a key that refuses NULL has no first row to point at"),
        (RootlessLink, "a key that refuses NULL has no first row to point at"),
    ],
)
def test_a_self_key_no_shape_can_fill_is_a_comment_and_a_finding_not_a_tree(
    model: type[Model], why: str
) -> None:
    """``Table`` refuses a ``Tree`` here and a ``FanOut`` everywhere, so neither is written.

    The report does not raise either: the rest of the factory was measured
    and is worth reading, and a comment sits where the declaration would be.
    """
    link = "parent_id" if model is RootlessNode else "previous_id"
    calls = {"n": 0}

    def factory() -> Model:
        calls["n"] += 1
        # A required self-key is pointed at a row that is never committed: the
        # call runs in a transaction that is rolled back, and the constraint is
        # deferred, so nothing checks it. A unique one needs a new row each call.
        key = 1 if model is RootlessNode else calls["n"]
        return model._default_manager.create(**{link: key})

    report = shape_from_factory(factory, samples=10)

    field = link.removesuffix("_id")
    assert "Tree(" not in report
    assert "FanOut(" not in report
    assert f"    # {field}: no shape can fill this" in report
    assert f"{model.__name__}.{field}: {why}" in report
    assert f"{field}=" not in report
    # Being unique is no longer why nothing fills it.
    assert "a unique key cannot" not in report


def _report_over_a_self_key_no_shape_can_fill(model: type[Model]) -> str:
    """The report for a factory linking each row to the one before, or a required key to a row."""

    def factory() -> Model:
        if model is RootlessNode:
            # Never committed: the call is rolled back and the constraint deferred.
            return model.objects.create(parent_id=1)  # type: ignore[attr-defined]
        previous = model._default_manager.order_by("pk").last()
        return model._default_manager.create(previous=previous)

    return shape_from_factory(factory, samples=10)


def test_a_self_key_that_refuses_null_is_not_told_to_be_left_out() -> None:
    """``Table`` refuses ``RootlessNode`` when ``parent`` is left out, so that remedy is false.

    The remedy has to be one ``Table`` accepts: the column made ``null=True`` (after
    which leaving it out, or a ``Tree``, is a shape), or the table filled by a
    ``Projection`` with ``columns=`` and ``sql=``.
    """
    report = _report_over_a_self_key_no_shape_can_fill(RootlessNode)

    assert "Leave it out and fill it after the build" not in report
    assert "Make the column null=True, or fill the table with a Projection" in report
    # The sentence is read against what Table does, not taken on trust.
    with pytest.raises(InvalidShape, match="RootlessNode.parent cannot be null"):
        _built(report, RootlessNode)


@pytest.mark.parametrize("model", [Successor, Chain, Relay])
def test_a_nullable_unique_self_key_is_written_as_a_flat_tree_not_told_to_be_left_out(
    model: type[Model],
) -> None:
    # A tree that gives no row two children fills it, as FanOut(max_children=1)
    # fills a unique key to another table, so the comment and its remedy are gone.
    report = _report_over_a_self_key_no_shape_can_fill(model)

    assert "previous=Tree(roots=0.1, sizes=Constant(1))," in report
    assert "1 of 10 rows had no parent" in report
    assert "no shape can fill" not in report
    assert "Leave it out and fill it after the build." not in report
    assert "Zipf" not in report


@pytest.mark.parametrize("model", [Successor, Chain, Relay])
def test_a_report_over_a_unique_self_key_is_source_a_shape_builds(
    model: type[Model],
) -> None:
    # Through Shape and build rather than Table alone: Table cannot see a
    # constraint, so it accepted Chain and Relay even while Shape refused them.
    report = _report_over_a_self_key_no_shape_can_fill(model)

    table = _built(report, model, rows=200)

    assert isinstance(table.fields["previous"], Tree)
    build(Shape(table))
    links = [row for row in model._default_manager.values_list("previous_id", flat=True) if row]
    assert len(links) == 180
    assert len(links) == len(set(links))


@pytest.mark.parametrize("model", [Successor, Chain, Relay])
def test_a_unique_self_key_always_left_null_is_the_figure_table_offers_and_a_finding(
    model: type[Model],
) -> None:
    report = shape_from_factory(model._default_manager.create, samples=20)

    assert "previous=Tree(roots=0.05, sizes=Constant(1))," in report
    assert f"{model.__name__}.previous: every row was left without a parent" in report
    assert "sizes=Zipf()" not in report
    assert isinstance(_built(report, model).fields["previous"], Tree)
    Shape(_built(report, model))


@_NEEDS_NULLS_DISTINCT
def test_a_self_key_whose_nulls_count_as_one_is_still_a_comment_and_a_finding() -> None:
    # Shape refuses every tree on this column, flat or not, and the column left
    # out holds one NULL many times, so neither is what the report may offer.
    report = _report_over_a_self_key_no_shape_can_fill(TiedLink)

    assert "Tree(" not in report
    assert "    # previous: no shape can fill this" in report
    assert "TiedLink.previous: a constraint counting every NULL as one value" in report
    assert "Fill the table with a Projection." in report
    assert "Leave it out" not in report
    assert "null=True" not in report


def _chain_with_roots(n: int) -> int | None:
    """Every fourth call is a root and the rest point at the call before, so no parent has two."""
    return None if n % 4 == 1 else n - 2


def test_a_key_in_a_composite_unique_constraint_is_not_unique_on_its_own() -> None:
    """Fails without the whole-tuple match in ``_is_unique``: ``name in constraint.fields``.

    ``Folder`` is unique on ``(parent, name)``, which lets a parent have many
    children so long as their names differ, so ``parent`` alone is a tree's column.
    The factory repeats names; distinct ones would be written as a ``Sequential``
    that does not build, which is a separate defect.
    """
    report = shape_from_factory(_tree_factory(Folder, _chain_with_roots), samples=20)

    assert "parent=Tree(roots=0.25, sizes=Zipf())" in report
    assert "no shape can fill" not in report


def test_a_conditional_unique_constraint_does_not_make_the_key_unique() -> None:
    """Fails without ``constraint.condition is None`` in ``_is_unique``."""
    report = shape_from_factory(_tree_factory(Sibling, _chain_with_roots), samples=20)

    assert "parent=Tree(roots=0.25, sizes=Zipf())" in report
    assert isinstance(_built(report, Sibling).fields["parent"], Tree)


def test_a_check_constraint_is_not_read_as_a_unique_one() -> None:
    """A check constraint has no ``fields``: reading it as a unique one raises.

    From Django 5.1 its predicate is its ``condition``, which is never ``None``,
    so ``condition is None`` short-circuits first there and this alone does not
    need ``isinstance``; the test below is the one that does.
    """
    report = shape_from_factory(_tree_factory(Checked, _chain_with_roots), samples=20)

    assert "parent=Tree(roots=0.25, sizes=Zipf())" in report
    assert isinstance(_built(report, Checked).fields["parent"], Tree)


def test_a_constraint_that_is_not_a_unique_one_is_not_read_as_one() -> None:
    """Fails without ``isinstance(constraint, UniqueConstraint)`` in ``_is_unique``.

    It has no ``fields`` and its ``condition`` is ``None``, so on every Django the
    unguarded read raises ``AttributeError``.
    """
    report = shape_from_factory(_tree_factory(Exclusive, _chain_with_roots), samples=20)

    assert "parent=Tree(roots=0.25, sizes=Zipf())" in report
    assert isinstance(_built(report, Exclusive).fields["parent"], Tree)


def test_a_relation_to_another_table_is_reported_exactly_as_before() -> None:
    """The tree branch must not touch a key to a different table, byte for byte."""
    companies = [Company.objects.create(name=f"c{i}") for i in range(4)]

    report = shape_from_factory(lambda: _shared_parent(companies), samples=20)

    assert report == (
        "# Measured from 20 calls. This is source to read and edit, not a shape to\n"
        "# build from: what it found is what your factory does, which is not the same\n"
        "# question as what your production data looks like.\n"
        "\n"
        "# What your factory does not vary, which is what this is for:\n"
        "#   - Session.company: every one of the 4 parents got exactly 5 row(s). A flat "
        "fan-out is the one shape in which a join estimate cannot miss, because the "
        "average is the truth -- declare the tail you actually have, and childless= for "
        "the parents nobody references.\n"
        "#   - Session.label: every row got 's'. The factory fixes this column, so the "
        "declaration above says the planner will see one value -- decide what the real "
        "spread is.\n"
        "\n"
        "Table(Session, rows=...,\n"
        "    company=FanOut(Zipf()),  # was flat: 4 parents, 5 row(s) each,\n"
        "    label=Constant('s'),\n"
        ")\n"
        "\n"
        "# Read at 20 samples. A larger run sees more of the tail, so the numbers above "
        "move with it."
    )


def _one_code(n: int) -> str:
    return "c"


def _named_factory(
    model: type[Model],
    name: Callable[[int], str],
    *,
    code: Callable[[int], str] = lambda n: f"k{n}",
) -> Callable[[], Model]:
    """A tree factory whose ``n``th row is called ``name(n)``, to make a column distinct or not.

    ``code`` is each row's own unless given: a model with a uniqueness over
    ``code`` needs one that does not repeat with ``name``.
    """
    made: list[Model] = []

    def factory() -> Model:
        n = len(made) + 1
        parent = made[n - 2] if n > 1 and _chain_with_roots(n) is not None else None
        row = model.objects.create(  # type: ignore[attr-defined]
            name=name(n), code=code(n), parent=parent
        )
        made.append(row)
        return row

    return factory


def _compiles(report: str) -> None:
    """The report is source: it has to parse, whatever it says."""
    compile(report, "<report>", "exec")


@pytest.mark.parametrize("model", [Folder, SpelledFolder, Shelf])
def test_source_over_columns_that_are_all_distinct_compiles(model: type[Model]) -> None:
    """Fails without the comma moved before the comment: it landed inside it.

    ``name=Sequential(...)  # every value distinct,`` left the next column
    without a separator, so a model with a column after it was a SyntaxError.
    The 0.25.1 tests all used repeated names, so none reached this line.
    """
    report = shape_from_factory(
        _named_factory(model, lambda n: f"n{n}", code=_one_code), samples=20
    )

    assert "name=Sequential(...),  # every value distinct\n" in report
    _compiles(report)


@pytest.mark.parametrize("model", [Folder, SpelledFolder, Shelf])
def test_source_over_a_column_with_many_values_compiles(model: type[Model]) -> None:
    """The other commented declaration of ``_describe``, which had the same defect."""
    report = shape_from_factory(
        _named_factory(model, lambda n: f"n{n % 15}", code=_one_code), samples=40
    )

    assert "name=Skew({...}),  # 15 distinct values, top share" in report
    _compiles(report)


def test_every_declaration_in_a_report_keeps_its_comma_outside_its_comment() -> None:
    companies = [Company.objects.create(name=f"c{i}") for i in range(4)]
    report = shape_from_factory(lambda: _shared_parent(companies), samples=20)

    # A column without a comment, unchanged.
    assert "    label=Constant('s'),\n" in report
    _compiles(report)


@pytest.mark.parametrize(
    ("model", "uniqueness"),
    [
        (Folder, "one_name_per_folder"),
        (SpelledFolder, "one_name_per_spelled_folder"),
        (Shelf, "unique_together ('parent', 'name')"),
    ],
)
def test_a_self_key_under_a_composite_uniqueness_over_repeated_values_is_a_finding(
    model: type[Model], uniqueness: str
) -> None:
    """The report wrote ``Tree`` beside ``Skew`` with nothing said, and ``Shape`` refuses that."""
    report = shape_from_factory(
        _named_factory(model, lambda n: f"n{n % 3}", code=_one_code), samples=20
    )

    finding = next(
        line
        for line in report.splitlines()
        if line.startswith(f"#   - {model.__name__}.parent:") and uniqueness in line
    )
    assert "name is declared as a draw" in finding
    assert "Sequential" in finding
    assert "Projection" in finding
    if model is SpelledFolder:
        # check_constraints compares the spelling too, so Shape accepts this model
        # until that half of the attname fix lands; the finding is still owed.
        return
    # The finding is true: the source as written is the one Shape refuses.
    with pytest.raises(InvalidShape, match="is a Tree beside"):
        Shape(_built(report, model))


@pytest.mark.parametrize("model", [Folder, SpelledFolder, Shelf])
def test_a_self_key_under_a_composite_uniqueness_over_distinct_values_needs_no_finding(
    model: type[Model],
) -> None:
    """A pair is distinct as soon as either half is, so ``Sequential`` keeps the constraint."""
    report = shape_from_factory(
        _named_factory(model, lambda n: f"n{n}", code=_one_code), samples=20
    )

    assert "declared as a draw" not in report
    assert "one_name_per" not in report
    assert "unique_together" not in report


def test_a_self_key_beside_a_relation_in_the_uniqueness_is_still_a_finding() -> None:
    """A relation is written as a fan-out, never as a column distinct in every row.

    Fails without ``not field.is_relation`` in ``_is_distinct``: every call here
    gets a company of its own, so the values are all different, and a fan-out
    over them is still a draw.
    """
    made: list[Mirror] = []

    def factory() -> Mirror:
        row = Mirror.objects.create(
            company=Company.objects.create(name="c"), parent=made[-1] if made else None
        )
        made.append(row)
        return row

    report = shape_from_factory(factory, samples=20)

    assert "company=FanOut(Zipf())" in report
    assert "company is declared as a draw" in report


@pytest.mark.parametrize("model", [SpelledChain, SpelledRelay])
def test_a_uniqueness_spelled_with_the_attname_still_makes_the_self_key_unique(
    model: type[Model],
) -> None:
    """Fails without ``get_field(...).name``: ``previous_id`` is not ``previous``.

    Unread, the key got ``sizes=Zipf()``, which ``Shape`` refuses on a unique column.
    """
    report = _report_over_a_self_key_no_shape_can_fill(model)

    assert "previous=Tree(roots=0.1, sizes=Constant(1))," in report
    assert "Zipf" not in report
    Shape(_built(report, model))


def test_a_uniqueness_that_leaves_the_self_key_out_is_no_finding_about_it() -> None:
    """Fails without ``field.name in names`` in either loop of ``_composite_uniqueness``."""
    report = shape_from_factory(
        _named_factory(Coded, lambda n: f"n{n % 3}", code=lambda n: f"k{n // 3}"), samples=20
    )

    assert "unique together" not in report
    assert "one_code_per_name" not in report
    assert "unique_together" not in report


@pytest.mark.parametrize("model", [Chain, Relay, SpelledChain])
def test_a_uniqueness_over_the_self_key_alone_is_no_composite_finding(
    model: type[Model],
) -> None:
    """Fails without ``len(names) > 1``: a column alone is not unique *together* with nothing."""
    report = _report_over_a_self_key_no_shape_can_fill(model)

    assert "unique together" not in report
