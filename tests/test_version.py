"""The version is a public, single-sourced fact."""

from __future__ import annotations

import django_data_shape


def test_version_is_exported_and_single_sourced() -> None:
    from django_data_shape.version import __version__

    assert django_data_shape.__version__ == __version__
    assert __version__.count(".") == 2
