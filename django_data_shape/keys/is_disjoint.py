from __future__ import annotations

from django_data_shape.keys.disjoint import Disjoint


def is_disjoint(keys: object) -> bool:
    """Whether ``keys`` implements ``Disjoint`` and answers yes."""
    return isinstance(keys, Disjoint) and keys.is_disjoint_from_existing_rows()
