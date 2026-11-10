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
    # Without this, both tests below pass by finding nothing at all.
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
