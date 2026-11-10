from __future__ import annotations

import pytest

from django_data_shape import Constant, InvalidShape, Paired, Zipf


def test_it_reads_back_as_what_was_declared() -> None:
    paired = Paired("company", Zipf())

    assert paired.relation == "company"
    assert isinstance(paired.weights, Zipf)


@pytest.mark.parametrize("relation", ["", None, 7])
def test_a_pairing_without_a_named_relation_is_refused(relation: object) -> None:
    # Named rather than inferred, as PerParent names its relation: a reader should not guess.
    with pytest.raises(InvalidShape, match="needs the name of the fan-out"):
        Paired(relation, Zipf())  # type: ignore[arg-type]


def test_the_pairing_is_part_of_what_the_declaration_says() -> None:
    # Pairing over a different fan-out is a different edge table, so both reach the digest.
    assert Paired("company", Zipf()).canonical() != Paired("person", Zipf()).canonical()
    assert Paired("company", Zipf()).canonical() != Paired("company", Constant(1)).canonical()


def test_it_reads_back_in_a_repr() -> None:
    assert repr(Paired("company", Constant(1))) == "Paired('company', Constant(1))"
