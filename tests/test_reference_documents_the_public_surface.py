"""The reference page and ``__all__`` are one claim, so they are checked as one.

``docs/reference.md`` is hand-maintained, and drift in it is invisible: an entry
that names a symbol which is not exported still builds, because mkdocstrings
resolves the full dotted path and never consults ``__all__``. That is how three
documented functions came to be reachable only as
``django_data_shape.check_constraints.check_constraints`` while
``from django_data_shape import check_constraints`` silently handed back the
*module* of the same name -- an import that succeeds and then fails at the call
site with "module object is not callable". A missing entry drifts the other way
just as quietly: ``ShapeNotEmpty`` was raised by ``build`` and exported for
several releases without appearing on the page at all.

Neither direction can be caught by the docs build or by the type checker, so it
is caught here.
"""

from __future__ import annotations

import re
from pathlib import Path

import django_data_shape

# The page addresses every symbol by its full dotted path, so the documented
# name is the last component. Anchored to the line start because a ":::" inside
# a fenced example is documentation, not a directive.
_DIRECTIVE = re.compile(r"^::: +(?P<path>[\w.]+)$", re.MULTILINE)

# Pytest fixtures are provided by the plugin's entry point and are never
# imported from the package root, so they are documented without being
# exported. They are the only legitimate asymmetry.
_FIXTURE_PREFIX = "django_data_shape.fixtures."

_REFERENCE = Path(__file__).resolve().parent.parent / "docs" / "reference.md"


def _documented() -> dict[str, str]:
    """Every symbol the reference page renders, mapped to the path it used."""
    return {
        path.rsplit(".", 1)[1]: path
        for path in _DIRECTIVE.findall(_REFERENCE.read_text(encoding="utf-8"))
    }


def _exported() -> set[str]:
    """Every name in ``__all__`` that a reference entry could describe.

    ``__version__`` is a string, and mkdocstrings has nothing to render for it.
    """
    return {name for name in django_data_shape.__all__ if not name.startswith("__")}


def test_every_exported_symbol_appears_on_the_reference_page() -> None:
    missing = sorted(_exported() - set(_documented()))

    assert not missing, (
        f"exported but undocumented: {missing}. Add a '::: "
        "django_data_shape.<module>.<symbol>' entry to docs/reference.md, in "
        "the section its neighbours are in."
    )


def test_every_documented_symbol_is_actually_exported() -> None:
    """The direction that fails silently, because the docs build still passes."""
    documented = _documented()
    unexported = sorted(
        name
        for name, path in documented.items()
        if not path.startswith(_FIXTURE_PREFIX) and name not in _exported()
    )

    assert not unexported, (
        f"documented but not in __all__: {unexported}. A reader following the "
        "page will write 'from django_data_shape import <name>' and, where a "
        "module shares the name, receive the module instead of an error."
    )


def test_a_documented_name_resolves_to_the_symbol_and_not_its_module() -> None:
    """The failure the second test exists to prevent, asserted directly."""
    for name, path in _documented().items():
        if path.startswith(_FIXTURE_PREFIX):
            continue
        attribute = getattr(django_data_shape, name)

        assert attribute.__module__ == path.rsplit(".", 1)[0], (
            f"django_data_shape.{name} is not the symbol documented at {path}"
        )
