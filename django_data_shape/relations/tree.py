from __future__ import annotations

from django_data_shape.declaration.invalid_shape import InvalidShape
from django_data_shape.distributions.distribution import Distribution


class Tree:
    """Each row's parent is an earlier row of the same table, or nobody.

    The self-referential relation, like an org unit's parent unit, which ``FanOut`` cannot fill."""

    def __init__(self, *, roots: float, sizes: Distribution) -> None:
        if not isinstance(roots, (int, float)) or not 0 < roots < 1:
            raise InvalidShape(
                "Tree roots is the share of rows with no parent and must be in (0, 1), got "
                f"{roots!r}. The first row is always a root, since no earlier row exists for it "
                "to point at, so a tree cannot have none; and a share of 1 leaves every row a "
                "root, which is the column left out of the shape."
            )
        self._roots = float(roots)
        self._sizes = sizes

    @property
    def roots(self) -> float:
        return self._roots

    @property
    def sizes(self) -> Distribution:
        return self._sizes

    def canonical(self) -> object:
        """The root share and the size distribution. See ``Canonical``."""
        return (self._roots, self._sizes)

    def __repr__(self) -> str:
        return f"Tree(roots={self._roots!r}, sizes={self._sizes!r})"
