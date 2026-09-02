"""Raised when loaded data breaks a rule the declaration said it would keep."""

from __future__ import annotations


class InvariantViolated(Exception):
    """A declared invariant found rows that should not exist.

    Its own type rather than an :class:`~django_data_shape.invalid_shape.InvalidShape`,
    because it is a different kind of wrong. An invalid shape is a declaration
    that could not describe any database; a violated invariant is a declaration
    that described one and then did not build it. The first is answered by
    rewriting the declaration, the second by asking which of the two -- the rule
    or the generator -- is lying.

    **It fails the build**, and it does so inside the transaction that loaded
    the rows, so nothing lands. That is the more useful of the two readings: an
    invariant that failed the *test* would leave a database full of impossible
    data for every later assertion to be evaluated against, and those
    assertions would pass or fail for reasons unrelated to the code. A build
    that refuses leaves the database exactly as it was found.

    The message carries the rule's name, how many rows broke it and a sample of
    them, because a build failure is read out of a terminal rather than stepped
    through in a debugger -- and a rule that only says it was violated has
    handed the reader back the work it just did.
    """
