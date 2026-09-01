"""Keys the caller supplies, checked rather than trusted."""

from __future__ import annotations

import itertools

import pytest

from django_data_shape import InvalidShape, KeyFunction


def test_it_uses_the_function() -> None:
    keys = KeyFunction(lambda row: f"page-{row:04d}")

    assert keys.key_for(7, stream=0) == "page-0007"


def test_a_function_that_is_not_pure_is_refused() -> None:
    counter = itertools.count()

    # Checked on a sample rather than trusted. A key that varies between calls
    # breaks reproducibility silently, in the one column every foreign key
    # points at.
    with pytest.raises(InvalidShape, match="pure function of the row index"):
        KeyFunction(lambda row: next(counter))


def test_a_function_that_repeats_itself_is_refused() -> None:
    with pytest.raises(InvalidShape, match="duplicate keys"):
        KeyFunction(lambda row: row % 4)


def test_it_reads_back_as_the_function_it_wraps() -> None:
    def page_key(row: int) -> str:
        return f"page-{row}"

    assert repr(KeyFunction(page_key)) == "KeyFunction('page_key')"
