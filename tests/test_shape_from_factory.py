"""Reading a factory, and reporting what it does not vary.

The tests are about the **findings**, not the declaration. Inference is the easy
half and it is not what this is for: a factory measured faithfully produces the
flat world this package exists to argue against, so the value is in saying so.
"""

from __future__ import annotations

import pytest
from django.db import connection

from django_data_shape import InvalidShape, shape_from_factory
from tests.testapp.models import Company, Project, Session

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="it runs a factory against a database and rolls it back",
    ),
]


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
