"""The pytest surface, deliberately outside the top-level re-exports.

Everything in here imports ``pytest``, and ``pytest`` is an optional extra
rather than a dependency. Re-exporting these from ``django_data_shape/__init__``
would make ``import django_data_shape`` fail for a project that runs its suite
with Django's own runner, so the import boundary is drawn where the dependency
boundary already is: ``from django_data_shape.fixtures import shape_fixture``
says out loud that pytest is required for this half and not for the other.
"""

from django_data_shape.fixtures.scale_fixture import scale_fixture
from django_data_shape.fixtures.shape_fixture import shape_fixture
from django_data_shape.fixtures.skip_unless_postgres import skip_unless_postgres

__all__ = [
    "scale_fixture",
    "shape_fixture",
    "skip_unless_postgres",
]
