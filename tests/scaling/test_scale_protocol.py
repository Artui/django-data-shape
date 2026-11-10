from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
_TY = Path(sys.executable).parent / "ty"
_CONSUMERS = _ROOT / "tests" / "scaling" / "scale_protocol_consumers.py"
_IMPOSTORS = _ROOT / "tests" / "scaling" / "scale_protocol_impostors.py"

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
    # A callable whose parameter has another name failed until factor went positional-only.
    result = _check(_CONSUMERS)

    assert result.returncode == 0, result.stdout + result.stderr


def test_and_the_ones_that_should_not_are_rejected() -> None:
    # The control.
    result = _check(_IMPOSTORS)

    assert result.returncode != 0
    assert "keyword_only" in result.stdout
    assert "not_a_context_manager" in result.stdout
