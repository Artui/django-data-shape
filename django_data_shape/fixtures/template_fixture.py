"""A template, built or derived, as the test database of a whole session."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.backends.base.creation import TEST_DATABASE_PREFIX

from django_data_shape.databases.clone_database import clone_database
from django_data_shape.fixtures.skip_unless_postgres import skip_unless_postgres


# ``object`` for the same reason as in shape_fixture: the type of a fixture is
# pytest's own and it changed shape between pytest 8.0 and 8.4.
def template_fixture(
    template: Callable[[str], str],
    *,
    using: str = DEFAULT_DB_ALIAS,
    strategy: str | None = "file_copy",
) -> object:
    """A session-scoped pytest fixture that makes the test database a clone of a template.

    The pytest face of
    :func:`~django_data_shape.databases.derive_template.derive_template`, and of
    :func:`~django_data_shape.databases.template_database.template_database`
    alone: the way a test reaches a world the application finished, which
    :func:`~django_data_shape.fixtures.shape_fixture.shape_fixture` cannot, since
    it builds straight into the test database and a step needs a template to
    run over. ``template`` is a callable taking the connection alias and
    returning a template's name, so a plain template, one started from a base
    and a chain of derived ones are all written with the functions that make
    them, and nothing here repeats their arguments. Bind it in ``conftest.py``::

        from django_data_shape import derive_template, template_database
        from django_data_shape.fixtures import template_fixture

        def finished_world(using: str) -> str:
            template = template_database(SHAPE, using=using, base="myproject_base")
            return derive_template(template, replay_draws, key="replay-draws-3", using=using)

        world = template_fixture(finished_world)

    and request the name from a test, or from an autouse session fixture so the
    world is there before the first test that touches the database::

        @pytest.fixture(scope="session", autouse=True)
        def _finished(world): ...

    It **composes with pytest-django rather than replacing it**, as
    ``shape_fixture`` does. It requests ``django_db_setup``, so the test database
    has been made -- under whatever name pytest-django gave it, the xdist
    worker's suffix included -- before the template is asked for, and then
    replaces that database with a clone of the template under the same name:
    the connection is closed and the clone made with ``replace=True``. Every
    test after that reads the template's rows and statistics, and
    pytest-django's teardown drops the clone as it would have dropped its own.
    ``template`` is called inside ``django_db_blocker.unblock()``, once per
    session and worker; the template is built or derived on the first call on a
    machine and found on every one after, so what a session pays is the clone.

    **Session scope is load-bearing**, for the reason it is for
    ``shape_fixture``: pytest makes higher-scoped fixtures first, so the clone
    is in place before the function-scoped ``db`` fixture opens the transaction
    that wraps a test, and everything a test writes is rolled back over it.

    Hands back the template's name, which is what a test asserting on the world it
    was handed, or on its key, can use.

    **Only a test database is replaced.** pytest-django makes one only for the
    aliases a collected test asks for, so in a run whose tests touch no
    database the alias still names the configured one -- a developer's own --
    and the fixture hands back the template's name and leaves it alone. The
    same goes for a ``django_db_setup`` that makes no test database, as
    pytest-django's recipe for an existing one does: the template is not
    cloned into a database the settings do not call a test database, which is
    ``TEST["NAME"]`` when that is set and the configured name with Django's
    ``test_`` prefix otherwise.

    **What is worth knowing**, because each is a consequence of replacing the
    database rather than filling it:

    - **The world appears when the fixture is first made.** A test that ran
      earlier in the session saw the empty database pytest-django made, and a
      test that runs later sees this world whether it asked for it or not.
      Requesting it from an autouse session fixture makes it the database of
      every test.
    - **It replaces whatever was there.** A ``shape_fixture`` world already
      built on the same alias goes with the database it was built into. Put
      that shape into the template instead.
    - **The database pytest-django made first is thrown away**, migrations and
      all. ``--reuse-db`` keeps it between runs, so the migrations run once;
      a project whose migrations are what is slow can instead override
      ``django_db_setup`` itself, as the statistics page shows, and never make
      the database it would replace.
    - **A test marked ``django_db(transaction=True)`` truncates every table**
      at teardown and takes the world with it, as it does a ``shape_fixture``
      one. ``serialized_rollback=True`` is not a way out here: pytest-django
      serializes the test database as ``django_db_setup`` makes it, before
      this replaces it, so what it restores is the empty database.

    ``strategy`` is the clone's, as for
    :func:`~django_data_shape.databases.clone_database.clone_database`:
    ``file_copy`` by default, ``None`` on a PostgreSQL older than 15. It is not
    passed to ``template``, which takes its own.

    On a connection that cannot carry a shaped database the fixture skips with
    the reason rather than raising, as ``shape_fixture`` does, so a suite that
    also runs on SQLite reports what it did not check.
    """

    # The blocker is typed loosely for the reason shape_fixture gives: naming
    # its class would mean importing pytest_django.
    @pytest.fixture(scope="session")
    def templated_database(django_db_setup: None, django_db_blocker: Any) -> str:
        connection = connections[using]
        skip_unless_postgres(connection, "Cloning a template into the test database")
        with django_db_blocker.unblock():
            name = template(using)
            target = connection.settings_dict["NAME"]
            # pytest-django makes test databases only for the aliases a
            # collected test asks for, so in a run whose tests touch no
            # database, or under a django_db_setup that makes none, the alias
            # still names the configured database, and replacing it would drop
            # a developer's own
            # (test_a_database_pytest_django_did_not_make_is_left_alone).
            if not _is_test_database(connection.settings_dict):
                return name
            # Every connection this thread holds to the test database, not only
            # the alias's own: PostgreSQL refuses to drop a database anything is
            # attached to, and a mirror alias names the same one. On a warm
            # machine nothing before this has closed the alias pytest-django
            # left open (every test in test_template_fixture errors without it).
            for alias in connections:
                if connections[alias].settings_dict["NAME"] == target:
                    connections[alias].close()
            clone_database(name, target, using=using, strategy=strategy, replace=True)
        return name

    return templated_database


def _is_test_database(settings_dict: dict[str, Any]) -> bool:
    """Whether a connection names the test database Django's test setup gives it.

    ``TEST["NAME"]`` when that is set, as pytest-django sets it under xdist, and
    Django's ``test_`` prefix on the configured name otherwise. A configured name
    that already starts with ``test_`` passes the second check: the settings say
    nothing more, and Django would call its own test copy ``test_test_...``.
    """
    test_name = settings_dict.get("TEST", {}).get("NAME")
    if test_name:
        return settings_dict["NAME"] == test_name
    return str(settings_dict["NAME"]).startswith(TEST_DATABASE_PREFIX)
