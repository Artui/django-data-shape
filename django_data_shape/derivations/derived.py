"""A column computed by the caller's own function."""

from __future__ import annotations

from collections.abc import Callable

from django_data_shape.derivations.scope import Scope
from django_data_shape.invalid_shape import InvalidShape


class Derived:
    """``compute`` over the named sources, in the named scope.

    The general case, and the one every other face is a shorthand for. A
    consumer who wants "call my own code to fill this column" wants exactly
    this, which is why it is the mechanism rather than a fourth thing beside
    three correlation primitives: built separately, custom logic and correlation
    become two vocabularies that overlap on the interesting half.

    ```python
    total = Derived("quantity", "unit_price", compute=operator.mul)
    ```

    ``scope`` is the parameter, not a family of classes. The default reads other
    columns of the same row; ``Scope.PARENT`` reaches the row across a declared
    ``FanOut`` and ``Scope.RANK`` reads a shared rank. So a consumer's own
    function can correlate with the parent without this package shipping a face
    for their particular correlation:

    ```python
    region = Derived("account.country", compute=region_of, scope="parent")
    ```

    **Your function may not touch the database.** That is not a request: the
    generation pass runs under a wrapper on the connection being built, and a
    query raises
    :class:`~django_data_shape.derivation_queried_database.DerivationQueriedDatabase`
    naming the table. The rule is what keeps this a derivation rather than the
    per-row creation hook this package exists to replace -- a hook whose body
    can query is a hook whose body will, and then nothing is ``COPY``-loaded and
    this is a slow fixtures library with extra vocabulary.

    ``compute`` receives the resolved sources positionally, in the order they
    were declared, and nothing else. Not the row index, and not a draw: a
    function of the row index is a
    :class:`~django_data_shape.distributions.sequential.Sequential` and a
    function of a draw is a distribution, and both of those are already
    planner-visible declarations. Handing a derivation the same inputs would
    make it possible to write a distribution that the planner-facing half of
    this package cannot see.
    """

    def __init__(
        self,
        *sources: str,
        compute: Callable[..., object],
        scope: Scope | str = Scope.ROW,
    ) -> None:
        if not sources:
            raise InvalidShape(
                "Derived needs at least one source; a column computed from nothing is a "
                "Constant, which says the same thing without a callable."
            )
        if not callable(compute):
            raise InvalidShape(f"Derived compute must be callable, got {compute!r}.")
        try:
            self._scope = Scope(scope)
        except ValueError:
            # Named rather than passed through, because ValueError's own message
            # lists the enum's repr and not the words a declaration would use.
            raise InvalidShape(
                f"Derived scope must be one of {', '.join(member.value for member in Scope)}, "
                f"got {scope!r}."
            ) from None
        self._sources = sources
        self._compute = compute

    @property
    def scope(self) -> Scope:
        return self._scope

    @property
    def sources(self) -> tuple[str, ...]:
        return self._sources

    def value(self, row: int, draw: float, sources: tuple[object, ...]) -> object:
        return self._compute(*sources)

    def __repr__(self) -> str:
        name = getattr(self._compute, "__name__", self._compute)
        sources = ", ".join(repr(source) for source in self._sources)
        return f"Derived({sources}, compute={name!r}, scope={self._scope.value!r})"
