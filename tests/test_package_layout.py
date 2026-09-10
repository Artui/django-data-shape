"""The package root is a table of contents, not a drawer.

The structural rules this package is written to governed *files* -- one exported
symbol each, named after it -- and every one of them was obeyed while forty-two
modules piled up at the package root. A rule about files cannot notice a rule
about directories being broken, and prose about directories would have rotted
the same way, so the directory rule is asserted here instead.

Two failures are caught. **A new module at the root** is the drift the rule
exists to stop: the allowlist below is short and deliberate, and a module that
does anything belongs in the subpackage named for the concern it serves.
**A root module sharing a name with one inside a subpackage** is the other half,
and it is not hypothetical in this family -- a sibling repo grew two modules
twice each, once at the root and once under ``types/``, while every file rule
held.
"""

from __future__ import annotations

from pathlib import Path

import django_data_shape

# Everything else lives in a subpackage. ``utils.py`` is the one behavioural
# file allowed here, and only because its contents are used across concerns; a
# helper wanted by one subpackage belongs in that subpackage.
_ALLOWED_AT_THE_ROOT = {"__init__", "utils", "version"}

_PACKAGE = Path(django_data_shape.__file__).resolve().parent


def _modules(directory: Path) -> set[str]:
    return {path.stem for path in directory.glob("*.py")}


def _subpackages() -> list[Path]:
    return sorted(
        path for path in _PACKAGE.iterdir() if path.is_dir() and (path / "__init__.py").exists()
    )


def test_the_package_is_where_this_test_thinks_it_is() -> None:
    # Without this, both tests below pass by finding nothing at all, which is the
    # failure mode of every test that discovers its own inputs.
    assert _modules(_PACKAGE) >= _ALLOWED_AT_THE_ROOT
    assert len(_subpackages()) >= 4


def test_the_root_holds_only_the_allowlisted_modules() -> None:
    unexpected = sorted(_modules(_PACKAGE) - _ALLOWED_AT_THE_ROOT)

    assert not unexpected, (
        f"modules at the package root: {unexpected}. The root holds only "
        f"{sorted(_ALLOWED_AT_THE_ROOT)}; everything this package does lives in "
        "a subpackage named for the concern it serves. Move it into an existing "
        "one, or start a new one once three modules share the concern."
    )


def test_no_module_name_is_used_at_two_levels() -> None:
    """A name that appears twice makes a reader guess which one an import meant."""
    seen: dict[str, str] = {name: "the package root" for name in _modules(_PACKAGE)}
    collisions: list[str] = []
    for subpackage in _subpackages():
        for name in sorted(_modules(subpackage)):
            if name == "__init__":
                continue
            where = f"{subpackage.name}/"
            if name in seen:
                collisions.append(f"{name} (in {seen[name]} and in {where})")
            else:
                seen[name] = where

    assert not collisions, (
        f"module names used more than once: {collisions}. One name means one "
        "module here, so that a reader who sees the name knows which file it is."
    )
