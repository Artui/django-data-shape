"""What a projection means, decided before a connection is opened."""

from __future__ import annotations

import django
import pytest
from django.db import connection, models

from django_data_shape import (
    InvalidShape,
    KeyFunction,
    Md5Keys,
    Projection,
    SequentialKeys,
    SqlKeys,
    UuidKeys,
)
from tests.testapp.models import (
    Company,
    DeliveryDocument,
    DualSession,
    Event,
    EventSession,
    Order,
    Rehearsal,
    Session,
    SparseSession,
    TemplateSession,
    TokenSession,
    UuidSession,
)


def _sessions() -> Projection:
    return Projection(EventSession, per=Event, copying=TemplateSession)


def _statement(projection: Projection, seed: int = 0) -> str:
    # The connection is only ever asked to quote a name, which every backend
    # does, so this reads the same statement on Postgres and on SQLite.
    return projection.statement(connection, seed)[0]


def test_the_join_is_derived_from_the_model_graph() -> None:
    statement = _statement(_sessions())

    # Event and TemplateSession are joined through the template they both point
    # at. Nothing in the declaration says so; the edge is in _meta already.
    assert 'ON "src"."template_id" = "per"."template_id"' in statement
    assert f'FROM "{Event._meta.db_table}" AS "per"' in statement
    assert f'INNER JOIN "{TemplateSession._meta.db_table}" AS "src"' in statement


def test_every_kind_of_column_is_decided_and_the_column_list_is_stable() -> None:
    statement, params = _sessions().statement(connection, 0)

    columns, select = statement.split(" SELECT ", 1)
    # Sorted by name, like Table.columns(), so two declarations differing only
    # in the model's field order produce the same statement and hash alike.
    assert columns.endswith('("id", "event_id", "minutes", "source_id", "title", "channel")')
    # The edge the rows hang off, a copied-by-name column, the copied row's own
    # key, and a model default as a bound parameter rather than as SQL.
    assert '"per"."id"' in select
    assert '"src"."minutes"' in select
    assert '"src"."id"' in select
    assert params == ("web",)
    # note is nullable and is left out of the statement entirely.
    assert '"note"' not in statement


def test_the_key_comes_from_the_strategy_rather_than_from_the_statement() -> None:
    # The keys question, answered where it can be seen: the expression is the
    # strategy's own arithmetic over a row index the database computes, not a
    # sequence and not something the projection invented.
    assert isinstance(SequentialKeys(), SqlKeys)
    assert SequentialKeys().key_sql(7, "r") == "(r) + 1"
    assert "(row_number() OVER (ORDER BY " in _statement(_sessions())
    assert ") - 1) + 1" in _statement(_sessions())


def test_the_ordering_is_deterministic_and_is_the_arrival_order() -> None:
    statement = _statement(_sessions())

    # Twice on purpose: the window's ORDER BY is what makes the key
    # deterministic, the outer one is what decides where the rows land.
    assert statement.count('"per"."id", "src"."id"') == 2
    assert statement.endswith('ORDER BY "per"."id", "src"."id"')


def test_two_renderings_of_one_declaration_agree() -> None:
    assert _statement(_sessions()) == _statement(_sessions())


def test_a_different_seed_leaves_a_sequential_key_alone() -> None:
    # SequentialKeys ignores its stream in SQL exactly as it does in Python, so
    # the seed reaching key_sql must not silently change the keys.
    assert _statement(_sessions(), seed=1) == _statement(_sessions(), seed=2)


def test_it_reports_what_it_reads() -> None:
    assert _sessions().reads == (Event, TemplateSession)
    assert repr(_sessions()) == "Projection(EventSession, per=Event, copying=TemplateSession)"


def test_a_projection_with_neither_form_is_refused() -> None:
    with pytest.raises(InvalidShape, match="per= and copying="):
        Projection(EventSession)


def test_a_projection_missing_half_the_derived_form_is_refused() -> None:
    with pytest.raises(InvalidShape, match="per= and copying="):
        Projection(EventSession, per=Event)


def test_columns_without_sql_is_refused() -> None:
    with pytest.raises(InvalidShape, match="columns= without sql="):
        Projection(EventSession, per=Event, copying=TemplateSession, columns=("id",))


def test_params_without_sql_is_refused() -> None:
    with pytest.raises(InvalidShape, match="params= without sql="):
        Projection(EventSession, per=Event, copying=TemplateSession, params=("x",))


def test_a_projection_reading_itself_is_refused() -> None:
    with pytest.raises(InvalidShape, match="projected from itself"):
        Projection(EventSession, per=Event, copying=EventSession)


def test_a_projection_making_one_row_per_itself_is_refused() -> None:
    with pytest.raises(InvalidShape, match="projected from itself"):
        Projection(EventSession, per=EventSession, copying=TemplateSession)


def test_a_projected_model_with_no_edge_to_per_is_refused() -> None:
    # Order has no foreign key at all, so there is nothing for the projected
    # rows to hang off.
    with pytest.raises(InvalidShape, match="no foreign key to Company"):
        Projection(Order, per=Company, copying=Session)


def test_more_than_one_edge_to_per_is_refused_by_name() -> None:
    # DualSession points at Event twice, so which edge the projected rows hang
    # off is not something the model graph can answer.
    with pytest.raises(InvalidShape, match="more than one foreign key to Event") as raised:
        Projection(DualSession, per=Event, copying=TemplateSession)

    assert "event, replaces" in str(raised.value)


def test_an_unjoinable_pair_is_refused() -> None:
    # Session hangs off Company, but nothing Company points at is also pointed
    # at by Order, so there is no collection to copy along.
    with pytest.raises(InvalidShape, match="cannot see how those two are joined"):
        Projection(Session, per=Company, copying=Order)


def test_an_ambiguous_join_is_refused_and_names_both_models() -> None:
    # Rehearsal shares both a template and a venue with Event, so which
    # collection is being copied is genuinely undecidable.
    with pytest.raises(InvalidShape, match="more than one model") as raised:
        Projection(EventSession, per=Event, copying=Rehearsal)

    assert "Template" in str(raised.value)
    assert "Venue" in str(raised.value)


def test_a_column_nothing_can_fill_is_refused_by_name() -> None:
    with pytest.raises(InvalidShape, match="SparseSession.headcount") as raised:
        Projection(SparseSession, per=Event, copying=TemplateSession)

    assert "sql=" in str(raised.value)


def test_a_callable_default_is_refused_by_name() -> None:
    with pytest.raises(InvalidShape, match="TokenSession.token") as raised:
        Projection(TokenSession, per=Event, copying=TemplateSession)

    # The reason a projection has on top of Table's: there is no per-row moment
    # at which the callable could be called.
    assert "never pass through" in str(raised.value)


def test_a_key_strategy_with_no_sql_form_is_refused_by_name() -> None:
    with pytest.raises(InvalidShape, match="cannot assign its keys") as raised:
        Projection(UuidSession, per=Event, copying=TemplateSession)

    message = str(raised.value)
    assert "UuidKeys()" in message
    assert not isinstance(UuidKeys(), SqlKeys)
    # A refusal that names no remedy leaves a UUID-keyed projection looking
    # unsupported, when it is one declaration away. Md5Keys is that declaration,
    # and the message says why it is a different strategy rather than this one
    # gaining a SQL half.
    assert "keys=Md5Keys()" in message


def test_the_remedy_the_refusal_names_is_accepted() -> None:
    """The refusal above is a signpost, so what it points at has to be reachable."""
    Projection(UuidSession, per=Event, copying=TemplateSession, keys=Md5Keys())


def test_a_declared_strategy_with_no_sql_form_is_refused_too() -> None:
    # Passing one explicitly has to meet the same bar as inferring one, or the
    # refusal only holds for callers who did not try to work around it.
    with pytest.raises(InvalidShape, match="cannot assign its keys"):
        Projection(
            EventSession, per=Event, copying=TemplateSession, keys=KeyFunction(lambda row: row)
        )


def test_an_uninferrable_key_is_refused_before_the_sql_question() -> None:
    # A key type nothing infers a strategy for is refused for that reason first,
    # rather than for the SQL one -- the caller has not chosen a strategy yet,
    # so telling them the one they did not choose has no SQL form says nothing.
    class SlugSession(models.Model):
        code = models.CharField(max_length=20, primary_key=True)
        event = models.ForeignKey(Event, on_delete=models.CASCADE)
        title = models.CharField(max_length=50)

        class Meta:
            app_label = "testapp"

    with pytest.raises(InvalidShape, match="only integer and UUID keys are inferred"):
        Projection(SlugSession, per=Event, copying=TemplateSession)


@pytest.mark.skipif(django.VERSION < (5, 2), reason="composite primary keys arrived in Django 5.2")
def test_a_composite_primary_key_is_refused_as_arity_not_type() -> None:
    # The same refusal Table makes, reached through the other entry point: a
    # composite key has no column of its own, so it is not among the concrete
    # fields and the obvious lookup raises a bare StopIteration.
    class CompositeSession(models.Model):
        pk = models.CompositePrimaryKey("left_id", "right_id")
        left_id = models.IntegerField()
        right_id = models.IntegerField()

        class Meta:
            app_label = "testapp"

    with pytest.raises(InvalidShape, match="arity, not type"):
        Projection(CompositeSession, per=Event, copying=TemplateSession)


def test_a_raw_projection_takes_the_statement_as_given() -> None:
    projection = Projection(
        EventSession,
        columns=("id", "event", "title"),
        sql="SELECT 1, 2, %s",
        params=("x",),
    )
    statement, params = projection.statement(connection, 0)

    assert statement.endswith('("id", "event_id", "title") SELECT 1, 2, %s')
    assert params == ("x",)
    assert projection.reads == ()
    assert repr(projection) == (
        "Projection(EventSession, columns=('id', 'event_id', 'title'), sql=...)"
    )


def test_a_raw_projection_declaring_per_is_refused() -> None:
    with pytest.raises(InvalidShape, match="one form or the other"):
        Projection(EventSession, per=Event, sql="SELECT 1", columns=("id",))


def test_a_raw_projection_declaring_keys_is_refused() -> None:
    with pytest.raises(InvalidShape, match="sql= together with keys="):
        Projection(EventSession, sql="SELECT 1", columns=("id",), keys=SequentialKeys())


def test_a_raw_projection_without_columns_is_refused() -> None:
    with pytest.raises(InvalidShape, match="sql= without columns="):
        Projection(EventSession, sql="SELECT 1")


def test_a_raw_projection_naming_a_column_the_model_has_not_got_is_refused() -> None:
    with pytest.raises(InvalidShape, match="no field named nonesuch"):
        Projection(EventSession, sql="SELECT 1", columns=("id", "nonesuch"))


def test_a_raw_projection_leaving_the_key_out_is_refused() -> None:
    with pytest.raises(InvalidShape, match="missing from columns=") as raised:
        Projection(EventSession, sql="SELECT 1", columns=("event", "title"))

    assert "sequence" in str(raised.value)


def test_a_projection_takes_a_statistics_target_for_a_column_it_writes() -> None:
    projection = Projection(
        EventSession, per=Event, copying=TemplateSession, statistics={"title": 250}
    )

    assert projection.statistics == {"title": 250}


def test_a_statistics_target_on_a_field_the_model_does_not_have_is_refused() -> None:
    with pytest.raises(InvalidShape, match="EventSession has no field named nope"):
        Projection(EventSession, per=Event, copying=TemplateSession, statistics={"nope": 250})


def test_a_statistics_target_on_a_column_the_statement_leaves_out_is_refused() -> None:
    # note is nullable and is not a column TemplateSession carries, so the
    # derived statement leaves it out entirely: every projected row would hold
    # the same nothing, and a bigger sample of it would describe nothing.
    with pytest.raises(InvalidShape, match="does not write that column"):
        Projection(EventSession, per=Event, copying=TemplateSession, statistics={"note": 250})


def test_a_statistics_target_is_checked_against_a_statement_the_caller_wrote_too() -> None:
    # The same rule read off columns= rather than off the derived plan, which is
    # the only thing this package knows about a select it did not write.
    written = Projection(
        EventSession,
        columns=("id", "event", "title", "minutes"),
        sql="SELECT 1, 1, 'x', 1",
        statistics={"title": 250},
    )

    assert written.statistics == {"title": 250}
    with pytest.raises(InvalidShape, match="does not write that column"):
        Projection(
            EventSession,
            columns=("id", "event", "title", "minutes"),
            sql="SELECT 1, 1, 'x', 1",
            statistics={"channel": 250},
        )


def test_a_target_outside_postgres_own_range_is_refused_here_as_well() -> None:
    with pytest.raises(InvalidShape, match="collect no statistics"):
        Projection(EventSession, per=Event, copying=TemplateSession, statistics={"title": 0})


def test_a_raw_projection_can_name_what_it_selects_from() -> None:
    # Nothing here parses SQL, so a raw statement used to be a black box that
    # could only be ordered last. reads= is how it rejoins the graph: after what
    # it names, and before whatever fans out over the table it fills.
    projection = Projection(
        EventSession,
        columns=("id", "event", "title"),
        sql="SELECT e.id, e.id, e.name FROM testapp_event e",
        reads=(Event,),
    )

    assert projection.reads == (Event,)


def test_reads_without_a_statement_of_your_own_is_refused() -> None:
    # A derived projection already names its two inputs, so reads= would be a
    # second and quieter answer to a question per= and copying= have answered.
    with pytest.raises(InvalidShape, match="declares reads= without sql="):
        Projection(EventSession, per=Event, copying=TemplateSession, reads=(Company,))


def test_a_raw_projection_naming_itself_in_reads_is_refused() -> None:
    # order_tables carries no self-edge guard on purpose, because every way of
    # writing one is refused where the declaration is made. reads= was a new way
    # to write one, and a declaration that has to come after itself would have
    # been reported as a cycle from inside the ordering pass instead.
    with pytest.raises(InvalidShape, match="names itself in reads="):
        Projection(EventSession, columns=("id",), sql="SELECT 1", reads=(EventSession,))


def test_what_a_raw_projection_reads_is_part_of_the_declaration() -> None:
    # It writes no column and still decides the data: the same statement run
    # before and after a table selects different rows, so two declarations
    # differing only in reads= must not share a cached template database.
    without = Projection(EventSession, columns=("id",), sql="SELECT 1")
    with_reads = Projection(EventSession, columns=("id",), sql="SELECT 1", reads=(Event,))

    assert without.canonical() != with_reads.canonical()


def test_projecting_into_an_inherited_model_is_refused_too() -> None:
    # The escape hatch is the first thing a reader reaches for once Table has
    # refused, so it has to refuse for the same reason rather than accept and
    # then insert into a column the child's table has not got.
    with pytest.raises(InvalidShape, match="multi-table inheritance"):
        Projection(DeliveryDocument, columns=("id",), sql="SELECT 1")
