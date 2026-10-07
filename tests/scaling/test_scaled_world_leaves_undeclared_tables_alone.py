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
from django_data_shape import (
    Constant,
    FanOut,
    KeyFunction,
    Md5Keys,
    Projection,
    SequentialKeys,
    Shape,
    Table,
    UuidKeys,
    Zipf,
    build,
    scaled_world,
)
from tests.testapp.models import (
    Club,
    Company,
    Depot,
    Event,
    KeyedSession,
    MemberFee,
    OptionalChild,
    Region,
    Section,
    Session,
    SessionNote,
    ShortCode,
    SlugPk,
    Template,
    TemplateSession,
    UuidSession,
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
    message = " ".join(str(refused.value).split())
    # The ways out that work, each named: declare the table, or do not make
    # the rows.
    assert (
        "Declare testapp_memberfee in the shape too, so those rows are the world's; or" in message
    )
    assert "do not create those rows" in message
    # And not Disjoint keys, which a section's integer key cannot take:
    # following that advice failed at the load, a UUID out of range for bigint
    # on PostgreSQL and too large for an INTEGER on SQLite.
    assert "Disjoint" not in message
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


def _refusal(shape: Shape, alias: str = "default") -> str:
    """The ``ShapeReferenced`` message entering a world over ``shape`` raises."""
    with pytest.raises(Exception) as refused, contextlib.ExitStack() as entering:
        entering.enter_context(scaled_world(shape, 1, using=alias))
    assert refused.type is django_data_shape.ShapeReferenced
    return " ".join(str(refused.value).split())


def _callers_region(alias: str) -> Region:
    region = Region.objects.using(alias).create(name="caller")
    Depot.objects.using(alias).create(region=region, name="caller")
    return region


def _regions_by_sequence() -> Table:
    # A UUID key with keys this package counts out rather than digests, so the
    # declared regions are emptied -- and Disjoint keys are a way out.
    return Table(Region, rows=2, keys=SequentialKeys(), name=Constant("world"))


@pytest.mark.parametrize("alias", _ALIASES)
def test_disjoint_keys_are_offered_for_a_uuid_key_that_does_not_have_them(alias: str) -> None:
    _callers_region(alias)

    message = _refusal(Shape(_regions_by_sequence(), seed=5), alias)

    assert "(testapp_depot.region_id -> testapp_region)" in message
    assert (
        "give testapp_region Disjoint keys (UuidKeys or Md5Keys), so the world builds beside "
        "the rows already there instead of emptying the table; or do not create those rows"
    ) in message


@pytest.mark.parametrize("alias", _ALIASES)
def test_disjoint_keys_are_not_offered_unless_every_named_table_can_take_them(alias: str) -> None:
    # Two declared tables, two references: the regions could take Disjoint
    # keys and the sections could not, so giving them to the regions would
    # leave the fee's reference to a section refused all the same.
    _callers_region(alias)
    club = _callers_club(alias, fee=True)

    message = _refusal(Shape(_regions_by_sequence(), *_sections_of(club).tables, seed=5), alias)

    assert "testapp_depot.region_id -> testapp_region" in message
    assert _REFERENCE in message
    assert "Disjoint" not in message


@pytest.mark.parametrize("alias", _ALIASES)
def test_disjoint_keys_are_not_offered_for_a_table_that_has_them(alias: str) -> None:
    # The sessions are UUID-keyed and Disjoint already. They are emptied only
    # because they point at events the world empties, so the caller's note on
    # one is refused -- and advice to give them the keys they have would send
    # the reader nowhere.
    template = Template.objects.using(alias).create(name="caller")
    event = Event.objects.using(alias).create(template=template, name="caller")
    session = UuidSession.objects.using(alias).create(event=event, title="caller")
    SessionNote.objects.using(alias).create(session=session, text="caller")
    shape = Shape(
        Table(Template, rows=1, name=Constant("world")),
        Table(Event, rows=2, template=FanOut(Zipf()), name=Constant("world")),
        Table(UuidSession, rows=3, event=FanOut(Zipf()), title=Constant("world")),
        seed=5,
    )

    message = _refusal(shape, alias)

    assert "(testapp_sessionnote.session_id -> testapp_uuidsession)" in message
    assert "Disjoint" not in message


@pytest.mark.skipif(connection.vendor != "postgresql", reason="a projection is PostgreSQL's")
def test_disjoint_keys_are_not_offered_for_a_projected_table() -> None:
    # A projected table's keys are never read by a scaled world, which empties
    # it whatever they are -- so Md5Keys, a Disjoint strategy that also fits
    # its UUID key, would change nothing. A plain table rather than a model
    # holds the reference, because no model in the suite points at this one.
    template = Template.objects.create(name="caller")
    event = Event.objects.create(template=template, name="caller")
    session = KeyedSession.objects.create(event=event, title="caller", minutes=1)
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE shape_keyed_note (session_id uuid REFERENCES testapp_keyedsession (id) "
            "DEFERRABLE INITIALLY DEFERRED)"
        )
        cursor.execute("INSERT INTO shape_keyed_note VALUES (%s)", [session.pk])
    shape = Shape(
        Table(Template, rows=1, name=Constant("world")),
        Table(
            TemplateSession,
            rows=2,
            template=FanOut(Zipf()),
            title=Constant("world"),
            minutes=Constant(1),
        ),
        Table(Event, rows=2, template=FanOut(Zipf()), name=Constant("world")),
        Projection(KeyedSession, per=Event, copying=TemplateSession, keys=Md5Keys()),
        seed=5,
    )

    message = _refusal(shape)

    assert "(shape_keyed_note.session_id -> testapp_keyedsession)" in message
    assert "Disjoint" not in message


@pytest.mark.skipif(connection.vendor != "postgresql", reason="the reference is built in SQL")
@pytest.mark.parametrize(("model", "offered"), [(SlugPk, True), (ShortCode, False)])
def test_disjoint_keys_are_offered_for_a_character_key_that_holds_them(
    model: type[SlugPk | ShortCode], offered: bool
) -> None:
    # What the two strategies accept is whatever the primary key's own field
    # accepts from a UUID. A character key with room for its 36 characters
    # does -- both strategies load into SlugPk's on either backend -- so the
    # advice is offered there, where a UUIDField-only rule would withhold it;
    # one with room for eight does not, and the advice is withheld.
    table = model._meta.db_table
    model.objects.create(code="caller", name="caller")
    with connection.cursor() as cursor:
        cursor.execute(
            f"CREATE TABLE shape_code_note (code varchar(50) REFERENCES {table} (code) "
            "DEFERRABLE INITIALLY DEFERRED)"
        )
        cursor.execute("INSERT INTO shape_code_note VALUES ('caller')")
    shape = Shape(
        Table(model, rows=2, keys=KeyFunction(lambda row: f"code-{row}"), name=Constant("world")),
        seed=5,
    )

    message = _refusal(shape)

    assert f"(shape_code_note.code -> {table})" in message
    assert (f"give {table} Disjoint keys (UuidKeys or Md5Keys)" in message) is offered
    assert ("Disjoint" in message) is offered


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


@pytest.mark.skipif(connection.vendor != "postgresql", reason="the constraint is dropped in SQL")
def test_a_reference_the_database_does_not_enforce_is_not_seen() -> None:
    # What a ForeignKey(db_constraint=False) leaves in the database, made here
    # by dropping the fee's constraint inside the test's transaction: a column
    # holding a section's key and nothing saying so. A GenericForeignKey is the
    # same case. The refusal reads the constraints, so it does not see this
    # one, and the caller's fee ends up pointing at a section the world built
    # under the key the caller's section had.
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, "testapp_memberfee")
        (foreign_key,) = (name for name, info in constraints.items() if info["foreign_key"])
        cursor.execute(
            f"ALTER TABLE testapp_memberfee DROP CONSTRAINT {connection.ops.quote_name(foreign_key)}"
        )
    club = _callers_club("default", fee=True)
    (section,) = Section.objects.values_list("pk", flat=True)

    with scaled_world(_sections_of(club), 1):
        fee = MemberFee.objects.get()
        assert fee.section_id == section
        assert Section.objects.get(pk=section).name == "world"

    assert _callers_rows("default") == [[("caller", None)], [("caller", "caller")], [("caller", 7)]]


def _cascading(table: str, column: str) -> None:
    """Re-create ``table``'s key into the companies ``ON DELETE CASCADE``.

    What Django 6.1's ``DB_CASCADE`` puts in the database, made in SQL inside
    the test's transaction so it needs no model and runs on the Django floor.
    Deferred like every key Django creates. The catalogue is read back, so a
    statement that did not take cannot leave these tests passing over a key
    that cascades nothing.
    """
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, table)
        (foreign_key,) = (name for name, info in constraints.items() if info["foreign_key"])
        quoted = connection.ops.quote_name(foreign_key)
        cursor.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT {quoted}, ADD CONSTRAINT {quoted} "
            f"FOREIGN KEY ({column}) REFERENCES testapp_company (id) "
            "ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED"
        )
        cursor.execute("SELECT confdeltype FROM pg_constraint WHERE conname = %s", [foreign_key])
        assert cursor.fetchone() == ("c",)


@pytest.mark.skipif(connection.vendor != "postgresql", reason="the key is re-created in SQL")
def test_a_cascading_key_from_an_undeclared_table_is_refused_before_anything_is_removed() -> None:
    # A DELETE of the companies would take the caller's session with it
    # through this key, and the session's table is not declared. The refusal
    # reads the reference first, so no DELETE runs at all.
    _cascading("testapp_session", "company_id")
    company = Company.objects.create(name="caller")
    Session.objects.create(company=company, label="caller")
    shape = Shape(Table(Company, rows=2, name=Constant("world")), seed=5)

    with (
        CaptureQueriesContext(connection) as captured,
        pytest.raises(Exception) as refused,
        contextlib.ExitStack() as entering,
    ):
        entering.enter_context(scaled_world(shape, 1))

    assert refused.type is django_data_shape.ShapeReferenced
    assert "(testapp_session.company_id -> testapp_company)" in str(refused.value)
    assert not [query for query in captured if query["sql"].startswith(("DELETE", "TRUNCATE"))]
    assert list(Session.objects.values_list("company__name", "label")) == [("caller", "caller")]


@pytest.mark.skipif(connection.vendor != "postgresql", reason="the keys are re-created in SQL")
def test_a_declared_child_under_a_cascading_key_is_emptied_with_its_parent() -> None:
    # The DELETE route, which an undeclared row left null puts the world on.
    # The sessions are declared, so whatever the companies' DELETE could reach
    # through their key is a declared row being removed anyway -- and the
    # sessions go first. The optional children cascade too, and keep their row,
    # because a key left null references nothing for a DELETE to follow.
    _cascading("testapp_session", "company_id")
    _cascading("testapp_optionalchild", "company_id")
    company = Company.objects.create(name="caller")
    Session.objects.create(company=company, label="caller")
    OptionalChild.objects.create(company=None, label="caller")
    shape = Shape(
        Table(Company, rows=2, name=Constant("world")),
        Table(Session, rows=4, label=Constant("world"), company=FanOut(Zipf())),
        seed=5,
    )

    with CaptureQueriesContext(connection) as captured, scaled_world(shape, 1) as rows:
        assert rows == 6
        assert sorted(Company.objects.values_list("name", flat=True)) == ["world"] * 2
        assert (
            list(Session.objects.values_list("company__name", "label")) == [("world", "world")] * 4
        )
        assert list(OptionalChild.objects.values_list("company_id", "label")) == [(None, "caller")]

    assert [query["sql"] for query in captured if query["sql"].startswith("DELETE")] == [
        'DELETE FROM "testapp_session"',
        'DELETE FROM "testapp_company"',
    ]
    assert list(Session.objects.values_list("company__name", "label")) == [("caller", "caller")]
    assert list(Company.objects.values_list("pk", "name")) == [(company.pk, "caller")]


def _delete_trigger_on_companies(timing: str, body: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE FUNCTION shape_on_delete() RETURNS trigger LANGUAGE plpgsql AS "
            f"$$BEGIN {body} END$$"
        )
        cursor.execute(
            f"CREATE TRIGGER shape_on_delete {timing} DELETE ON testapp_company "
            "FOR EACH ROW EXECUTE FUNCTION shape_on_delete()"
        )


@pytest.mark.skipif(connection.vendor != "postgresql", reason="the trigger is PL/pgSQL")
def test_a_delete_trigger_on_a_declared_table_runs_and_is_rolled_back() -> None:
    # The DELETE route fires row-level triggers, which TRUNCATE never did: an
    # audit trigger on a declared table writes into a table the shape does not
    # declare, inside the block. The world's own statements still change no
    # such table, and the trigger's write goes with the rollback.
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE shape_audit (id bigint)")
    _delete_trigger_on_companies("AFTER", "INSERT INTO shape_audit VALUES (OLD.id); RETURN OLD;")
    company = Company.objects.create(name="caller")
    OptionalChild.objects.create(company=None, label="caller")

    def audited() -> list[int]:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM shape_audit")
            return [row[0] for row in cursor.fetchall()]

    with scaled_world(Shape(Table(Company, rows=2, name=Constant("world"))), 1):
        assert audited() == [company.pk]

    assert audited() == []
    assert list(Company.objects.values_list("pk", "name")) == [(company.pk, "caller")]


@pytest.mark.skipif(connection.vendor != "postgresql", reason="the trigger is PL/pgSQL")
def test_a_delete_trigger_that_keeps_its_rows_leaves_the_build_refused() -> None:
    # A BEFORE DELETE trigger returning null is how a soft delete is written,
    # and it keeps the row the world's DELETE meant to remove. The build then
    # meets a table that is not empty and refuses it, as it would any other.
    _delete_trigger_on_companies("BEFORE", "RETURN NULL;")
    company = Company.objects.create(name="caller")
    OptionalChild.objects.create(company=None, label="caller")

    with (
        pytest.raises(Exception, match="testapp_company already holds rows") as refused,
        contextlib.ExitStack() as entering,
    ):
        entering.enter_context(
            scaled_world(Shape(Table(Company, rows=2, name=Constant("world"))), 1)
        )

    assert refused.type is django_data_shape.ShapeNotEmpty
    assert list(Company.objects.values_list("pk", "name")) == [(company.pk, "caller")]


def test_off_postgresql_a_composite_key_counts_when_any_column_is_set() -> None:
    # The one place the two readings of a reference differ. PostgreSQL's
    # catalogue reads a composite foreign key whole, so one column null means
    # no reference (the composite test above); introspection reports each
    # column on its own, so here the column that is set is a reference. Django
    # never creates a composite key, so it is documented rather than read.
    sqlite = connections["not_postgres"]
    company = Company.objects.using("not_postgres").create(name="caller")
    with sqlite.cursor() as cursor:
        cursor.execute("CREATE UNIQUE INDEX shape_pair_key ON testapp_company (id, name)")
        cursor.execute(
            "CREATE TABLE shape_pair (company_id bigint, company_name varchar(200), "
            "FOREIGN KEY (company_id, company_name) REFERENCES testapp_company (id, name))"
        )
        cursor.execute("INSERT INTO shape_pair VALUES (%s, NULL)", [company.pk])
    try:
        message = _refusal(Shape(Table(Company, rows=2, name=Constant("world"))), "not_postgres")
    finally:
        with sqlite.cursor() as cursor:
            cursor.execute("DROP TABLE shape_pair")
            cursor.execute("DROP INDEX shape_pair_key")

    assert "(shape_pair.company_id -> testapp_company)" in message
