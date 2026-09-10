"""Raised when a declaration cannot describe a database."""

from __future__ import annotations


class InvalidShape(Exception):
    """A shape declaration is contradictory, incomplete or unsatisfiable.

    Its own type, and raised as early as the contradiction can be seen -- at
    declaration time wherever possible, rather than at load time. The reason is
    the package's own bar: a generated database that is wrong is worse than one
    that refuses to exist, because the test suite it feeds will assert on data
    that could never occur and pass or fail for reasons unrelated to the code.

    Every message names the model, the field or the constraint at fault. An
    error that says only that something is inconsistent leaves the reader to
    re-derive what this code already knew.
    """
