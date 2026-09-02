"""One shape, offered at whatever size the test asks for."""

from __future__ import annotations

from functools import partial

import pytest
from django.db import DEFAULT_DB_ALIAS, connections

from django_data_shape.fixtures.skip_unless_postgres import skip_unless_postgres
from django_data_shape.scale_protocol import ScaleProtocol
from django_data_shape.scaled_world import scaled_world
from django_data_shape.shape import Shape


# ``object`` for the same reason as in shape_fixture: the type of a fixture is
# pytest's own and it changed shape between pytest 8.0 and 8.4.
def scale_fixture(shape: Shape, *, using: str = DEFAULT_DB_ALIAS) -> object:
    """A pytest fixture yielding a :class:`~django_data_shape.scale_protocol.ScaleProtocol`.

    The pytest face of the scale protocol. Bind it in ``conftest.py``::

        from django_data_shape import Constant, Shape, Table
        from django_data_shape.fixtures import scale_fixture

        world = scale_fixture(Shape(Table(Order, rows=100, status=Constant("new"))))

    and a growth assertion has somewhere to ask for a bigger world::

        def test_the_dashboard_query_is_constant(world, django_assert_num_queries):
            for factor in (1, 10):
                with world(factor):
                    with django_assert_num_queries(3):
                        dashboard()

    The declared row counts are the world at factor 1, so the base declaration
    should be the smallest world that still means something -- a hundred rows
    against a thousand is the regime this is for, and it is milliseconds per
    factor. Size, in the two-million-row sense that makes a query *plan*
    realistic, is a different assertion with a different cost and does not vary
    a factor at all.

    Function-scoped, and it requests pytest-django's ``db`` fixture, which does
    two things worth knowing. A test using this needs no ``django_db`` marker of
    its own. And each world is then built inside the transaction that wraps the
    test, so tearing it down is a savepoint rollback: cheap, exact, and leaving
    the test's own transaction usable afterwards. A test that marks itself
    ``transaction=True`` still works -- the marker wins over the fixture, and
    the rollback is then an ordinary one.

    Skips with a stated reason where a shaped database cannot exist, so a suite
    that also runs on SQLite reports the growth assertions it did not make.
    """

    @pytest.fixture
    def scaled_worlds(db: None) -> ScaleProtocol:
        skip_unless_postgres(connections[using], "Building a shape at a scale factor")
        # Binding, not wrapping. The fixture's whole job is to attach one shape
        # and one connection to the protocol; everything about what a world is
        # and how it is undone belongs to scaled_world, where a consumer not
        # using pytest can reach it too.
        return partial(scaled_world, shape, using=using)

    return scaled_worlds
