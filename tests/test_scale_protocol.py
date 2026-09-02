"""The protocol's promise is a type-level claim, so it takes a type-level test."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_TY = Path(sys.executable).parent / "ty"
_CONSUMERS = _ROOT / "tests" / "scale_protocol_consumers.py"
_IMPOSTORS = _ROOT / "tests" / "scale_protocol_impostors.py"

# Skipped rather than passed where the checker is absent. Every job that runs
# this suite syncs the dev group, so ty is there; an interpreter without it is a
# contributor running pytest by hand, and telling them the claim went unchecked
# is better than telling them it held.
pytestmark = pytest.mark.skipif(not _TY.exists(), reason="ty is not installed beside this python")


def _check(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_TY), "check", str(path)], cwd=_ROOT, capture_output=True, text=True, check=False
    )


def test_the_implementations_a_consumer_would_write_satisfy_the_protocol() -> None:
    # The reproduction that mattered: a hand-rolled callable whose parameter is
    # called something else. Before the factor was made positional-only this
    # failed, and the documentation promising it had no test behind it at all.
    result = _check(_CONSUMERS)

    assert result.returncode == 0, result.stdout + result.stderr


def test_and_the_ones_that_should_not_are_rejected() -> None:
    # The control. Point the checker at nothing and it reports nothing, so a
    # green run of the test above is only evidence if this one is red for the
    # file next door.
    result = _check(_IMPOSTORS)

    assert result.returncode != 0
    assert "keyword_only" in result.stdout
    assert "not_a_context_manager" in result.stdout
