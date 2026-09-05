"""A lone ``%`` in a supplied select, refused where the declaration is.

``sql=`` is a parameterised statement -- the caller writes ``%s`` and passes
``params=``, which is the documented interface -- so pyformat is part of that
contract and a literal ``%`` has to be doubled. That is correct and stays.

What was wrong is where it was discovered. The statement reaches the driver with
a parameter sequence, so an unescaped ``%`` is an incomplete placeholder, and
the exception comes from ``psycopg/cursor.py`` at build time naming nothing in
the shape. A caller who passed no parameters at all has no model that explains
it.

The asymmetry with ``SqlValue`` is deliberate and the refusal says so: on the
derived path the caller supplies no parameters and has no reason to know one
exists, so ``render()`` escapes for them.
"""

from __future__ import annotations

import pytest

from django_data_shape import Constant, FanOut, InvalidShape, Projection, Shape, Table, Uniform
from tests.testapp.models import Event, EventSession, Template

pytestmark = pytest.mark.django_db


def _projection(sql: str, params: tuple[object, ...] = ()) -> Projection:
    return Projection(
        EventSession,
        columns=("id", "event", "title"),
        sql=sql,
        params=params,
        reads=(Event,),
    )


def test_a_lone_percent_is_refused_at_declaration_time() -> None:
    with pytest.raises(InvalidShape) as caught:
        _projection("SELECT e.id, e.id, e.name FROM testapp_event e WHERE e.id % 5 = 0")

    message = str(caught.value)
    assert "EventSession" in message, "the refusal has to name the declaration"
    assert "%%" in message, "and say what to write instead"
    # Named, so the refusal cannot be mistaken for a complaint about the
    # placeholders the same statement is allowed to carry.
    assert "e.id % 5" in message


def test_the_refusal_explains_why_sqlvalue_differs() -> None:
    """The asymmetry is the thing a reader will next be confused by."""
    with pytest.raises(InvalidShape) as caught:
        _projection("SELECT e.id, e.id, e.name FROM testapp_event e WHERE e.id % 5 = 0")

    assert "SqlValue" in str(caught.value)


@pytest.mark.parametrize(
    "fragment",
    [
        "e.name <> %s",
        "e.name <> %(name)s",
        "e.name <> %s AND e.id % 5 = 0",
    ],
)
def test_a_placeholder_is_not_a_lone_percent(fragment: str) -> None:
    """Every pyformat shape survives, including one beside a doubled operator."""
    sql = f"SELECT e.id, e.id, e.name FROM testapp_event e WHERE {fragment.replace('% 5', '%% 5')}"
    params = ("x",) if "%s" in fragment or "%(name)s" in fragment else ()

    projection = _projection(sql, params)

    assert projection.model is EventSession


def test_a_doubled_percent_still_reaches_the_database_as_an_operator() -> None:
    """The refusal teaches a spelling that has to work, so it is run."""
    shape = Shape(
        Table(Template, rows=3, name=Constant("t")),
        Table(Event, rows=12, template=FanOut(Uniform(1, 3)), name=Constant("e")),
        Projection(
            EventSession,
            columns=("id", "event", "title", "minutes", "channel"),
            sql=(
                "SELECT row_number() OVER (ORDER BY e.id), e.id, "
                "cast(e.id %% 5 AS varchar(200)), 30, 'web' FROM testapp_event e"
            ),
            reads=(Event,),
        ),
        seed=5,
    )

    from django_data_shape import build

    build(shape, require_statistics=False)

    titles = {title for title in EventSession.objects.values_list("title", flat=True)}
    assert len(titles) > 1, "the operator has to have run, not been sent as a placeholder"
