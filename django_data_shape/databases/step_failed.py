from __future__ import annotations


class StepFailed(Exception):
    """The step given to ``derive_template`` raised, and nothing was kept.

    Raised from the step's own exception, which is its ``__cause__``, and names the key."""
