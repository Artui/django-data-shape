from __future__ import annotations


class InvariantViolated(Exception):
    """A declared invariant found rows that should not exist.

    Its own type rather than ``InvalidShape``: the declaration is fine, the loaded rows are not."""
