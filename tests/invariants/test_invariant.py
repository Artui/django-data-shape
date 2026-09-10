"""The declaration of a rule, and the four ways of writing one that will not do."""

from __future__ import annotations

import pytest
from django.db.models import Q

from django_data_shape import InvalidShape, Invariant
from tests.testapp.models import Project


def test_the_queryset_form_holds_the_model_and_the_rows_that_are_wrong() -> None:
    invariant = Invariant("no archived project is active", Project, violated_by=Q(status="ACTIVE"))

    assert invariant.name == "no archived project is active"
    assert invariant.model is Project
    assert invariant.violated_by == Q(status="ACTIVE")
    assert invariant.sql is None


def test_the_sql_form_names_its_own_tables() -> None:
    invariant = Invariant("no company has two", sql="SELECT 1 WHERE false")

    assert invariant.model is None
    assert invariant.violated_by is None
    assert invariant.sql == "SELECT 1 WHERE false"


def test_neither_spelling_is_refused() -> None:
    with pytest.raises(InvalidShape, match="was given neither"):
        Invariant("a rule that says nothing")


def test_both_spellings_are_refused() -> None:
    with pytest.raises(InvalidShape, match="was given both"):
        Invariant("two rules in one", Project, violated_by=Q(status="ACTIVE"), sql="SELECT 1")


def test_a_q_with_no_model_is_refused() -> None:
    with pytest.raises(InvalidShape, match="which model they are rows of"):
        Invariant("rows of what", violated_by=Q(status="ACTIVE"))


def test_a_sql_statement_with_a_model_beside_it_is_refused() -> None:
    # A declaration whose model is read by nothing is a declaration that lies
    # about what it checks.
    with pytest.raises(InvalidShape, match="would be read by nothing"):
        Invariant("a statement and a model", Project, sql="SELECT 1")


def test_a_q_is_rendered_faithfully_for_the_digest() -> None:
    # Unlike a callable, a Q is data: two of them printing alike filter alike,
    # so str is a rendering rather than an approximation.
    one = Invariant("rule", Project, violated_by=Q(status="ACTIVE"))
    other = Invariant("rule", Project, violated_by=Q(status="COMPLETE"))

    assert one.canonical() != other.canonical()
    assert one.canonical() == Invariant("rule", Project, violated_by=Q(status="ACTIVE")).canonical()


def test_it_reads_back_as_what_was_declared() -> None:
    assert repr(Invariant("one per company", sql="SELECT 1")) == (
        "Invariant('one per company', sql='SELECT 1')"
    )
    assert repr(Invariant("no active", Project, violated_by=Q(status="ACTIVE"))) == (
        "Invariant('no active', Project, violated_by=<Q: (AND: ('status', 'ACTIVE'))>)"
    )
