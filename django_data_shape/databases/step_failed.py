from __future__ import annotations


class StepFailed(Exception):
    """The step given to ``derive_template`` raised, and nothing was kept.

    Raised from the step's own exception, which is its ``__cause__``, so the
    traceback shows both: what went wrong inside the step, and which template and
    key it was deriving. The key is the part worth naming, because it is the only
    thing that tells one step from another -- a callable has no name a cache can
    trust, which is why ``derive_template`` asks for a key at all.

    Its own type rather than the step's exception passed through, because the
    step's exception says what failed and not where: a ``KeyError`` from a
    replay deep inside a session fixture reads the same as one from the test.
    Only an ``Exception`` is wrapped. A ``KeyboardInterrupt`` or ``SystemExit``
    is not a failure of the step, and goes on as it came.

    Nothing is left behind either way: the partial copy the step ran in is
    dropped, so the next call starts again from the parent template and runs the
    step again.
    """
