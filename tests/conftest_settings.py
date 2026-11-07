"""Django settings for the test suite.

The default backend is **Postgres**, not SQLite, and that is deliberate. This
package's own claim is that a performance assertion which passes because the
backend could not check it is worse than no assertion -- and the majority of what
it ships (``COPY`` loading, ``ANALYZE``, statistics, template-database reuse) is
unreachable on SQLite. A suite whose default run is green without ever touching a
planner would be the exact failure this library exists to expose.

So a contributor without a local Postgres gets a connection error rather than a
misleading pass. ``DATA_SHAPE_TEST_DATABASE=sqlite`` runs the portable half --
the vocabulary, the generator and the invariant arithmetic -- and CI runs both.
"""

from __future__ import annotations

import os

SECRET_KEY = "not-a-secret-this-is-the-test-suite"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_data_shape",
    "tests.testapp",
]

USE_TZ = True

_BACKEND = os.environ.get("DATA_SHAPE_TEST_DATABASE", "postgres")

if _BACKEND == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("PGDATABASE", "django_data_shape"),
            "USER": os.environ.get("PGUSER", ""),
            "PASSWORD": os.environ.get("PGPASSWORD", ""),
            "HOST": os.environ.get("PGHOST", "localhost"),
            "PORT": os.environ.get("PGPORT", "5432"),
        }
    }

# A second alias that is deliberately not Postgres. The backend gate can be
# unit-tested with a stub connection, and is -- but a stub proves the gate
# works, not that anything calls it. This alias is what lets one test drive the
# real entry point into a real refusal, which is the difference between
# asserting a guard exists and falsifying its absence.
DATABASES["not_postgres"] = {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": ":memory:",
}

# A third alias, with a database of its own, for the fixture that replaces a
# test database with a clone of a template. Its own NAME rather than the
# default's, because Django treats two aliases naming one database as one test
# database, and replacing it would replace the default's under every other test
# in the session. Off PostgreSQL it is SQLite, so the fixture's skip is what
# runs there. No test dependencies: Django orders every other alias after
# ``default`` unless told otherwise, and a test asking for this alias alone then
# reads as a circular dependency.
DATABASES["templated"] = {
    **(
        {**DATABASES["default"], "NAME": f"{DATABASES['default']['NAME']}_templated"}
        if _BACKEND != "sqlite"
        else {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    ),
    "TEST": {"DEPENDENCIES": []},
}

# A fourth alias that no test asks for, so pytest-django never makes a test
# database for it and its NAME stays the one configured here: the state of a
# developer's own database in a run whose tests touch none, which a fixture
# replacing a test database has to leave alone. No test may name it in a
# django_db mark, or pytest-django sets it up and the premise is gone.
DATABASES["unrequested"] = {
    **(
        {**DATABASES["default"], "NAME": f"{DATABASES['default']['NAME']}_unrequested"}
        if _BACKEND != "sqlite"
        else {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    ),
    "TEST": {"DEPENDENCIES": []},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
