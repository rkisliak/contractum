"""Derived version status: latest, preview, older (VER-03, VER-04)."""

from __future__ import annotations

import pytest
from dc_contracts.lifecycle import SemverParts, compare_semver, derive_status, parse_semver
from hypothesis import assume, given
from hypothesis import strategies as st

pytestmark = pytest.mark.unit


@st.composite
def version_texts(draw: st.DrawFn) -> str:
    core = ".".join(str(draw(st.integers(min_value=0, max_value=6))) for _ in range(3))
    pre = draw(st.lists(st.sampled_from(["alpha", "beta", "1", "2"]), max_size=2))
    return core + ("-" + ".".join(pre) if pre else "")


def _parsed(texts: list[str]) -> list[SemverParts]:
    out: list[SemverParts] = []
    for text in texts:
        parsed = parse_semver(text)
        assert parsed is not None
        out.append(parsed)
    return out


def _unique(rows: list[SemverParts]) -> list[SemverParts]:
    kept: list[SemverParts] = []
    for row in rows:
        if all(compare_semver(row, other) != 0 for other in kept):
            kept.append(row)
    return kept


@given(texts=st.lists(version_texts(), min_size=1, max_size=8), pick=st.integers(min_value=0))
def test_only_the_flagged_version_is_latest_above_it_is_preview_the_rest_older(
    texts: list[str], pick: int
) -> None:
    rows = _unique(_parsed(texts))
    latest = rows[pick % len(rows)]
    for row in rows:
        status = derive_status(row, latest, is_latest=row is latest)
        if row is latest:
            assert status == "latest"
        elif compare_semver(row, latest) > 0:
            assert status == "preview"
        else:
            assert status == "older"
    assert [
        row for row in rows if derive_status(row, latest, is_latest=row is latest) == "latest"
    ] == [latest]


@given(texts=st.lists(version_texts(), min_size=1, max_size=8))
def test_with_no_latest_every_version_is_a_preview(texts: list[str]) -> None:
    rows = _parsed(texts)
    assume(rows)
    assert {derive_status(row, None, is_latest=False) for row in rows} == {"preview"}


def test_prerelease_of_the_latest_core_is_older() -> None:
    latest = parse_semver("1.0.0")
    rc = parse_semver("1.0.0-rc.1")
    assert latest is not None
    assert rc is not None
    assert derive_status(rc, latest, is_latest=False) == "older"
