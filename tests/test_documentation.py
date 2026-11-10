from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)


def _documents() -> list[Path]:
    return [_ROOT / "README.md", *sorted((_ROOT / "docs").glob("*.md"))]


def test_the_documents_are_where_this_test_thinks_they_are() -> None:
    # Without this the parametrised test below passes by finding nothing.
    names = {path.name for path in _documents()}

    assert {"README.md", "index.md", "relations.md"} <= names


@pytest.mark.parametrize("document", _documents(), ids=lambda path: path.name)
def test_every_python_block_parses(document: Path) -> None:
    blocks = _BLOCK.findall(document.read_text())

    for number, block in enumerate(blocks, start=1):
        try:
            ast.parse(block)
        except SyntaxError as error:
            pytest.fail(f"{document.name} python block {number}: {error.msg}\n\n{block}")


def test_the_install_command_survives_a_shell_that_globs() -> None:
    # zsh expands square brackets and reports 'no matches found' on an unquoted extra.
    readme = (_ROOT / "README.md").read_text()

    assert "pip install 'django-data-shape[postgres]'" in readme
