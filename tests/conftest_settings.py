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

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
