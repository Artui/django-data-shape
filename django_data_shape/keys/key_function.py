"""Keys from a function the caller supplies."""

from __future__ import annotations

from collections.abc import Callable

from django_data_shape.invalid_shape import InvalidShape


class KeyFunction:
    """A caller's own deterministic mapping from row index to key.

    The escape hatch for a key this package cannot infer -- a natural key, a
    prefixed slug, an external identifier. Integer and UUID primary keys are
    inferred and need none of this; anything else is declared rather than
    guessed, because a guessed value in a semantic column is how a character
    primary key once got loaded with the strings "1", "2" and "3".

    The function must be a pure function of the row index. That is checked on a
    sample at construction rather than trusted: a key that varies between calls
    would break reproducibility, and it would break it silently, in the one
    column every foreign key points at.
    """

    def __init__(self, function: Callable[[int], object], *, sample: int = 64) -> None:
        first = [function(row) for row in range(sample)]
        if [function(row) for row in range(sample)] != first:
            raise InvalidShape(
                "KeyFunction must be a pure function of the row index, and this one returned "
                "different keys for the same rows on a second call. A key that varies between "
                "calls breaks reproducibility in the column every foreign key points at."
            )
        if len(set(first)) != len(first):
            raise InvalidShape(
                f"KeyFunction produced duplicate keys within its first {sample} rows, and a "
                "primary key has to be unique. It must be an injection from the row index."
            )
        self._function = function

    def key_for(self, row: int, stream: int) -> object:
        return self._function(row)

    def __repr__(self) -> str:
        return f"KeyFunction({getattr(self._function, '__name__', self._function)!r})"
