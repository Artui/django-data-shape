"""One shape, built once for a whole test session."""

from __future__ import annotations

from typing import Any

import pytest
from django.db import DEFAULT_DB_ALIAS, connections

from django_data_shape.build import build
from django_data_shape.build_result import BuildResult
from django_data_shape.fixtures.skip_unless_postgres import skip_unless_postgres
from django_data_shape.shape import Shape


# The return type is ``object`` on purpose. What ``pytest.fixture`` hands back
# is pytest's own -- a plain function on pytest 8.0 and a
# ``FixtureFunctionDefinition`` from 8.4 onwards -- so naming it here would pin
# this package's floor to a pytest version in exchange for nothing: the caller
# binds the value to a name and never calls it.
def shape_fixture(shape: Shape, *, using: str = DEFAULT_DB_ALIAS) -> object:
    """A session-scoped pytest fixture that builds ``shape`` once.

    Bind it to a name in ``conftest.py`` and request that name from a test::

        from django_data_shape import Constant, Shape, Table
        from django_data_shape.fixtures import shape_fixture

        orders = shape_fixture(Shape(Table(Order, rows=100_000, status=Constant("new"))))

    ::

        import pytest

        @pytest.mark.django_db
        def test_the_dashboard_query(orders):
            assert orders.rows == 100_000

    It **composes with pytest-django rather than replacing it**. The fixture
    requests ``django_db_setup``, which is the seam a project overrides to
    decide how its test database is made, so whatever a project has done there
    -- a template database, ``--reuse-db``, a different creation strategy -- has
    already happened before a row is generated. It then writes through
    ``django_db_blocker.unblock()``, the mechanism pytest-django documents for
    populating a database once. Neither of those is imported: they are asked for
    by name, so this package depends on two fixture names and not on
    pytest-django's internals.

    **Session scope is load-bearing, not a performance choice.** pytest creates
    higher-scoped fixtures before lower-scoped ones, so a session-scoped build
    always runs before the function-scoped ``db`` fixture opens the transaction
    that wraps a test -- which is what makes the rows committed and visible to
    every later test. A function-scoped build would be ordered against ``db`` by
    the accident of argument order, and on the losing side of that order it
    would be rolled back with the test that happened to build it.

    Yields the :class:`~django_data_shape.build_result.BuildResult`, so a test
    can assert on the size of the world it was handed.

    **One caveat, and it is worth stating plainly**: a test marked
    ``django_db(transaction=True)`` truncates every table at teardown, and takes
    the session's rows with it. Nothing rebuilds them, so a later test that
    reads this fixture is measuring an empty database. Keep transactional tests
    off the tables a shape owns, mark them ``serialized_rollback=True``, or
    build per test with
    :func:`~django_data_shape.scaled_world.scaled_world` at factor 1 -- which
    undoes itself and therefore does not care.

    **One world per table.** A session world holds its rows for the whole run,
    so :func:`~django_data_shape.fixtures.scale_fixture.scale_fixture` over the
    same model cannot build: the second build meets a table that is not empty
    and is refused. Give the two different models -- the session world the tables
    a plan assertion needs to be big, the scale harness the tables a growth
    assertion counts. It is the first thing a consumer composing both hits, and
    the refusal now names it.

    On a connection that cannot carry a shaped database the fixture skips with
    the reason rather than raising, so a suite that also runs on SQLite reports
    what it did not check instead of erroring or, worse, passing.
    """

    # The blocker is typed loosely because naming its class would mean importing
    # pytest_django, and the whole point of asking for it by name is that this
    # package does not. It is a pytest-django fixture; what is used of it is
    # ``unblock()``.
    @pytest.fixture(scope="session")
    def built_shape(django_db_setup: None, django_db_blocker: Any) -> BuildResult:
        # ``vendor`` is a class attribute, so the gate is read before anything
        # is unblocked and a skip costs no connection at all.
        skip_unless_postgres(connections[using], "Building a shape for the test session")
        with django_db_blocker.unblock():
            return build(shape, using=using)

    return built_shape
