"""A scaled world removes the rows of its declared tables and nothing else.

On PostgreSQL the emptying was ``TRUNCATE ... CASCADE``, and ``CASCADE`` follows
foreign keys by schema, transitively, whatever the rows hold. In an application
whose billing points back at its clubs, a shape declaring only a club's sections
reached the clubs themselves -- a fee references its section, a club references
its selected fee -- so a club the caller made was empty inside the block, and a
fan-out narrowed to it with ``parents=`` was refused for naming a key the
package's own statement had just removed. Without ``parents=`` nothing was
refused at all: tables the caller filled were silently empty for the block.

A world now refuses when a row it did not make references a row it has to
remove, and otherwise leaves every undeclared row where it was. Most tests run
on both aliases, because the refusal is made off PostgreSQL too, through
Django's introspection rather than PostgreSQL's catalogue.

The refusal is caught as ``Exception`` and its type checked afterwards, against
the package attribute rather than an import, so this module still collects on a
tree without the exception -- which is what lets these tests be replayed against
one and fail on their assertions rather than at import.
"""

from __future__ import annotations

import contextlib
import re

import pytest
from django.db import connection, connections
from django.test.utils import CaptureQueriesContext

import django_data_shape
from django_data_shape import Constant, FanOut, Shape, Table, UuidKeys, Zipf, build, scaled_world
from tests.testapp.models import (
    Club,
    Company,
    Depot,
    MemberFee,
    OptionalChild,
    Region,
    Section,
    Session,
)

pytestmark = pytest.mark.django_db(databases=["default", "not_postgres"])

# The default alias is PostgreSQL in the run that gates coverage and SQLite in
# the portable run; not_postgres is SQLite in both. Together they put the
# catalogue route and the introspection route through the same assertions.
_ALIASES = ["default", "not_postgres"]

_REFERENCE = "testapp_memberfee.section_id -> testapp_section"


def _sections_of(club: Club) -> Shape:
    return Shape(
        Table(
            Section,
            rows=3,
            name=Constant("world"),
            club=FanOut(Zipf(), parents=[club.pk]),
        ),
        seed=5,
    )


def _callers_club(alias: str, *, fee: bool, selected: bool = False) -> Club:
    """What a test's setup makes before entering a world: a club and a section.

    With ``fee``, a fee referencing that section too; with ``selected``, the
    club points back at it, closing the loop the module docstring describes.
    """
    club = Club.objects.using(alias).create(name="caller")
    section = Section.objects.using(alias).create(club=club, name="caller")
    if fee:
        made = MemberFee.objects.using(alias).create(section=section, amount=7)
        if selected:
            club.selected_fee = made
            club.save(using=alias)
    return club


def _callers_rows(alias: str) -> list[object]:
    return [
        list(Club.objects.using(alias).values_list("name", "selected_fee__amount")),
        list(Section.objects.using(alias).values_list("club__name", "name")),
        list(MemberFee.objects.using(alias).values_list("section__name", "amount")),
    ]


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_world_declaring_a_child_leaves_the_callers_parent_alone(alias: str) -> None:
    # The defect itself, on PostgreSQL: the CASCADE reached testapp_club through
    # the fees, and the fan-out below was refused for naming the club's key.
    club = _callers_club(alias, fee=False)

    with scaled_world(_sections_of(club), 1, using=alias) as rows:
        assert rows == 3
        assert list(Club.objects.using(alias).values_list("pk", "name")) == [(club.pk, "caller")]
        assert (
            list(Section.objects.using(alias).values_list("club_id", "name"))
            == [(club.pk, "world")] * 3
        )

    assert _callers_rows(alias) == [[("caller", None)], [("caller", "caller")], []]


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_fee_the_caller_made_refuses_the_world_naming_its_column(alias: str) -> None:
    # Removing the caller's section would leave the caller's fee pointing at
    # nothing, or take the fee with it, and the fee is not this world's.
    club = _callers_club(alias, fee=True, selected=True)

    with (
        pytest.raises(Exception, match=re.escape(_REFERENCE)) as refused,
        contextlib.ExitStack() as entering,
    ):
        entering.enter_context(scaled_world(_sections_of(club), 1, using=alias))

    assert refused.type is django_data_shape.ShapeReferenced
    message = str(refused.value)
    # The three ways out, each named: declare the table, make the keys
    # disjoint, or do not make the rows.
    assert "Declare testapp_memberfee in the shape too" in message
    assert "give testapp_section Disjoint keys" in message
    assert "do not create those rows" in message
    # Only references *into* a declared table. The club's own reference to the
    # fee is a row the world does not have to remove, so naming it would send
    # the reader after a table the world never touches.
    assert "selected_fee" not in message
    # And not the refusal the CASCADE used to provoke in its place.
    assert "parents=" not in message
    assert _callers_rows(alias) == [[("caller", 7)], [("caller", "caller")], [("caller", 7)]]


@pytest.mark.parametrize("alias", _ALIASES)
def test_declaring_the_fees_too_makes_their_rows_the_worlds(alias: str) -> None:
    # The first remedy the refusal names. Section and fee are both declared, so
    # the fee's reference to the section is between two tables the world
    # empties, and is not one to refuse.
    club = _callers_club(alias, fee=True)
    shape = Shape(
        *_sections_of(club).tables,
        Table(MemberFee, rows=6, amount=Constant(1), section=FanOut(Zipf())),
        seed=5,
    )

    with scaled_world(shape, 1, using=alias) as rows:
        assert rows == 9
        assert list(Club.objects.using(alias).values_list("name", flat=True)) == ["caller"]
        assert set(MemberFee.objects.using(alias).values_list("amount", flat=True)) == {1}

    assert _callers_rows(alias) == [[("caller", None)], [("caller", "caller")], [("caller", 7)]]


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_reference_left_null_is_no_reason_to_refuse(alias: str) -> None:
    # The shape declares the fees alone. The caller's club references
    # testapp_memberfee directly but holds null there, and the caller's section
    # references a non-null club -- a table the world reaches only through
    # the loop and never empties. Neither is a row the world has to remove.
    club = _callers_club(alias, fee=True)
    (section,) = Section.objects.using(alias).values_list("pk", flat=True)
    shape = Shape(
        Table(MemberFee, rows=2, amount=Constant(1), section=FanOut(Zipf(), parents=[section])),
        seed=5,
    )

    with scaled_world(shape, 1, using=alias):
        assert (
            list(MemberFee.objects.using(alias).values_list("section_id", "amount"))
            == [(section, 1)] * 2
        )
        assert Club.objects.using(alias).get().pk == club.pk

    assert _callers_rows(alias) == [[("caller", None)], [("caller", "caller")], [("caller", 7)]]


@pytest.mark.parametrize("alias", _ALIASES)
def test_disjoint_keys_build_beside_rows_that_reference_the_declared_table(alias: str) -> None:
    # The second remedy. A UUID-keyed table is not emptied at all, so the
    # caller's depot keeps referencing the caller's region inside the block.
    region = Region.objects.using(alias).create(name="caller")
    Depot.objects.using(alias).create(region=region, name="caller")

    shape = Shape(Table(Region, rows=2, keys=UuidKeys(), name=Constant("world")), seed=5)

    with scaled_world(shape, 1, using=alias):
        assert Region.objects.using(alias).count() == 3
        assert Depot.objects.using(alias).get().region_id == region.pk

    assert list(Region.objects.using(alias).values_list("name", flat=True)) == ["caller"]


@pytest.mark.parametrize("alias", _ALIASES)
def test_a_world_over_empty_tables_issues_no_emptying_statement(alias: str) -> None:
    # Nothing to remove, so nothing to fire either: no SET CONSTRAINTS, no
    # TRUNCATE and no DELETE -- only the read that found the tables empty.
    shape = Shape(
        Table(Company, rows=2, name=Constant("world")),
        Table(Session, rows=4, label=Constant("world"), company=FanOut(Zipf())),
        seed=5,
    )

    with CaptureQueriesContext(connections[alias]) as captured, scaled_world(shape, 1, using=alias):
        ...

    issued = [query["sql"].lstrip().upper() for query in captured]
    assert [
        sql for sql in issued if sql.startswith(("SET CONSTRAINTS", "TRUNCATE", "DELETE"))
    ] == []


@pytest.mark.skipif(connection.vendor != "postgresql", reason="TRUNCATE is the PostgreSQL route")
def test_over_a_session_world_the_emptying_is_one_truncate_naming_what_references_it() -> None:
    # The fast path, and the composition it exists for: a session world under a
    # scaled world declaring the same graph. Nothing outside the declaration
    # holds rows, so one TRUNCATE takes the declared tables and every table that
    # references them -- all empty, so nothing is lost -- and it is listed
    # rather than cascaded, so a list that missed one would be refused by
    # PostgreSQL instead of quietly widened.
    shape = Shape(
        Table(Company, rows=3, name=Constant("world")),
        Table(Session, rows=6, label=Constant("world"), company=FanOut(Zipf())),
        seed=5,
    )
    build(shape, require_statistics=False)

    with CaptureQueriesContext(connection) as captured, scaled_world(shape, 1) as rows:
        assert rows == 9

    emptying = [
        query["sql"] for query in captured if query["sql"].startswith(("TRUNCATE", "DELETE"))
    ]
    assert len(emptying) == 1
    (statement,) = emptying
    assert statement.startswith("TRUNCATE ")
    assert "CASCADE" not in statement
    # Declared, and undeclared but referencing a declared table.
    for table in ("testapp_company", "testapp_session", "testapp_project"):
        assert table in statement
    # A table nothing declared is reached from stays out of it.
    assert "testapp_club" not in statement


@pytest.mark.skipif(connection.vendor != "postgresql", reason="a composite key is built in SQL")
def test_a_composite_reference_with_one_column_null_references_nothing() -> None:
    # PostgreSQL checks a multi-column foreign key only when every column is
    # set (MATCH SIMPLE, its default), so a row with any of them null points at
    # nothing and the world may remove what it would otherwise point at.
    # A plain table rather than a temporary one, because PostgreSQL lets a
    # temporary table reference only temporary tables. DDL is transactional
    # there, so the test's own rollback drops it and the index.
    company = Company.objects.create(name="caller")
    with connection.cursor() as cursor:
        cursor.execute("CREATE UNIQUE INDEX shape_pair_key ON testapp_company (id, name)")
        cursor.execute(
            "CREATE TABLE shape_pair (company_id bigint, company_name varchar(200), "
            "FOREIGN KEY (company_id, company_name) REFERENCES testapp_company (id, name))"
        )
        cursor.execute("INSERT INTO shape_pair VALUES (%s, NULL)", [company.pk])

    with scaled_world(Shape(Table(Company, rows=2, name=Constant("world"))), 1):
        assert Company.objects.count() == 2
        assert not Company.objects.filter(name="caller").exists()

    assert list(Company.objects.values_list("name", flat=True)) == ["caller"]


@pytest.mark.skipif(connection.vendor != "postgresql", reason="the key is altered in SQL")
def test_the_delete_route_empties_children_before_parents() -> None:
    # Django's keys are deferred, so the order of the DELETEs is invisible to
    # them; a key checked at once is not, and a schema can have one. Here the
    # session's key is made immediate inside the test's transaction, and an
    # optional child left null puts the world on the DELETE route without
    # anything to refuse. Companies deleted before their sessions would fail on
    # the spot.
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, "testapp_session")
        (foreign_key,) = (name for name, info in constraints.items() if info["foreign_key"])
        cursor.execute(
            f"ALTER TABLE testapp_session ALTER CONSTRAINT {connection.ops.quote_name(foreign_key)} "
            "NOT DEFERRABLE"
        )
    company = Company.objects.create(name="caller")
    Session.objects.create(company=company, label="caller")
    OptionalChild.objects.create(company=None, label="caller")
    shape = Shape(
        Table(Company, rows=2, name=Constant("world")),
        Table(Session, rows=4, label=Constant("world"), company=FanOut(Zipf())),
        seed=5,
    )

    with CaptureQueriesContext(connection) as captured, scaled_world(shape, 1):
        assert OptionalChild.objects.get().label == "caller"

    assert [query["sql"] for query in captured if query["sql"].startswith("DELETE")] == [
        'DELETE FROM "testapp_session"',
        'DELETE FROM "testapp_company"',
    ]
    assert list(Session.objects.values_list("company__name", "label")) == [("caller", "caller")]


def test_a_key_from_a_parent_the_world_declares_is_named_as_the_worlds_doing() -> None:
    # The one way a caller's key can still vanish inside a world: its table is
    # declared in the same shape, so the world empties it before the fan-out
    # reads it. The refusal says so rather than only blaming the key. Two
    # caller rows and one world row, so the key named is never one the world
    # happens to rebuild.
    Club.objects.create(name="caller")
    club = Club.objects.create(name="caller")
    shape = Shape(
        Table(Club, rows=1, name=Constant("world")),
        *_sections_of(club).tables,
        seed=5,
    )

    with (
        pytest.raises(django_data_shape.InvalidShape, match="in parents=") as refused,
        contextlib.ExitStack() as entering,
    ):
        entering.enter_context(scaled_world(shape, 1))

    assert "if testapp_club is declared in the same shape" in " ".join(str(refused.value).split())
