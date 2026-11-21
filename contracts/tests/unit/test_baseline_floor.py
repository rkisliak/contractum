"""Baseline and stable major floor selection (VAL-02, VAL-03, D-146)."""

from __future__ import annotations

import pytest
from dc_contracts.compat import select_baseline
from dc_contracts.lifecycle import SemverParts, parse_semver

pytestmark = pytest.mark.unit


def rows(*versions: str) -> list[SemverParts]:
    parsed = [parse_semver(v) for v in versions]
    assert all(p is not None for p in parsed)
    return [p for p in parsed if p is not None]


def pick(*versions: str) -> tuple[str | None, int | None]:
    items = rows(*versions)
    index, floor = select_baseline(items)
    return (None if index is None else items[index].text), floor


def test_the_highest_precedence_published_row_is_the_baseline() -> None:
    assert pick("1.0.0", "2.0.0-rc.1") == ("2.0.0-rc.1", 1)


def test_an_unpublished_row_is_never_passed_so_the_caller_filters_it() -> None:
    # The caller passes published rows only: 1.1.0 (unpublished) is absent from the input.
    assert pick("1.0.0", "2.0.0-rc.1") == ("2.0.0-rc.1", 1)
    assert pick("1.0.0", "1.1.0") == ("1.1.0", 1)


def test_only_prereleases_leave_no_floor() -> None:
    assert pick("0.9.0-beta") == ("0.9.0-beta", None)


def test_no_rows_is_no_baseline_and_no_floor() -> None:
    assert select_baseline([]) == (None, None)


def test_precedence_not_input_order_decides() -> None:
    assert pick("2.0.0", "1.9.9", "2.0.0-rc.1") == ("2.0.0", 2)
    assert pick("1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-beta") == ("1.0.0-beta", None)


def test_the_floor_is_the_highest_stable_major_even_when_the_baseline_is_a_prerelease() -> None:
    assert pick("1.0.0", "3.0.0-rc.1", "2.4.0") == ("3.0.0-rc.1", 2)


def test_zero_major_is_a_floor_of_zero() -> None:
    assert pick("0.3.0") == ("0.3.0", 0)
