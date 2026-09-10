"""The realised fan-out as a mapping: what it answers and in which order."""

from __future__ import annotations

import pytest

from django_data_shape import ChildrenPerParent


def _counts(*pairs: tuple[object, int], null_share: float = 0.0) -> ChildrenPerParent:
    return ChildrenPerParent(
        [parent for parent, _size in pairs],
        [size for _parent, size in pairs],
        null_share,
    )


def test_it_is_an_ordinary_read_only_mapping() -> None:
    # The point of subclassing Mapping rather than inventing accessors: a caller
    # already knows how to read this, and everything below comes for free.
    counts = _counts((7, 3), (9, 0), (11, 12))

    assert counts[11] == 12
    assert len(counts) == 3
    assert 9 in counts
    assert 404 not in counts
    assert sum(counts.values()) == 15


def test_it_iterates_in_the_order_the_parent_keys_were_read() -> None:
    counts = _counts((11, 12), (7, 3), (9, 0))

    # Parent-key order, which is the order the partition was built over -- not
    # the ranking. Keeping the two apart is what stops "the largest group"
    # quietly becoming "the first group".
    assert list(counts) == [11, 7, 9]


def test_the_ranking_puts_the_head_first_and_the_tail_last() -> None:
    counts = _counts((7, 3), (9, 0), (11, 12))

    assert counts.ranked() == ((11, 12), (7, 3), (9, 0))
    assert counts.ranked()[0] == (11, 12)
    assert counts.ranked()[-1] == (9, 0)


def test_parents_with_the_same_count_keep_the_order_they_were_read_in() -> None:
    # Reproducible rather than merely deterministic: two builds of one shape
    # have to agree about which parent the head is, and a tie broken by
    # whichever way an unstable sort fell would make that depend on the sort.
    counts = _counts((5, 4), (6, 4), (7, 4))

    assert counts.ranked() == ((5, 4), (6, 4), (7, 4))


def test_the_childless_parents_are_named_rather_than_inferred() -> None:
    counts = _counts((7, 3), (9, 0), (11, 12), (13, 0))

    # In parent-key order, and only the genuinely unreferenced ones. Guessing
    # them off the tail of the ranking is how a test for outer-join behaviour
    # ends up using a parent that has three children.
    assert counts.childless() == (9, 13)


def test_a_fan_out_with_no_childless_tail_says_so_with_an_empty_tuple() -> None:
    assert _counts((7, 3), (11, 12)).childless() == ()


def test_the_null_share_is_carried_so_a_mismatch_can_be_explained() -> None:
    # Zero is the case where these numbers are row counts. Anything else and
    # they are the partition before it was thinned, so a caller comparing one
    # against a COUNT(*) needs to be able to see why the two differ from the
    # object that gave them the number.
    assert _counts((7, 3)).null_share == 0.0
    assert _counts((7, 3), null_share=0.25).null_share == 0.25


def test_sizes_that_do_not_line_up_with_the_parents_are_refused() -> None:
    # Both come out of one partition, so a length mismatch means the sizes were
    # computed over different parents from the ones being reported. Truncating
    # to the shorter of the two would report a partition nobody built.
    with pytest.raises(ValueError):
        ChildrenPerParent([1, 2, 3], [10, 20], 0.0)


def test_it_says_how_much_of_a_world_it_describes() -> None:
    assert repr(_counts((7, 3), (9, 0), (11, 12))) == "ChildrenPerParent(3 parents, 15 children)"
