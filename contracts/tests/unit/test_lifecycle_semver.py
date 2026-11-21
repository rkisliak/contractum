"""SemVer parsing, precedence and the publish checks hold for any input (VER-01, D-46, T-03-09)."""

from __future__ import annotations

import pytest
from dc_contracts.lifecycle import (
    MAX_SEMVER_PART,
    SemverParts,
    bump,
    compare_semver,
    parse_semver,
    validate_publish,
)
from dc_core.problems import ProblemError
from hypothesis import given
from hypothesis import strategies as st

pytestmark = pytest.mark.unit


def parts(text: str) -> SemverParts:
    parsed = parse_semver(text)
    assert parsed is not None, text
    return parsed


@pytest.mark.parametrize(
    "bad",
    ["1.0", "v1.0.0", "01.0.0", "", " 1.0.0", "1.0.0 ", 1.0, 1, None, "2147483648.0.0", "1.0.0-"],
)
def test_parse_semver_rejects_anything_but_a_strict_semver_string(bad: object) -> None:
    assert parse_semver(bad) is None


def test_parse_semver_accepts_the_integer_boundary() -> None:
    parsed = parse_semver("2147483647.0.0")
    assert parsed is not None
    assert parsed.major == MAX_SEMVER_PART
    assert parse_semver(f"0.{MAX_SEMVER_PART + 1}.0") is None
    assert parse_semver(f"0.0.{MAX_SEMVER_PART + 1}") is None


def test_build_metadata_is_ignored_for_equality() -> None:
    assert compare_semver(parts("1.0.0+a"), parts("1.0.0+b")) == 0


@pytest.mark.parametrize(
    ("lower", "higher"),
    [
        ("1.0.0-rc.1", "1.0.0"),
        ("1.0.0-rc.2", "1.0.0-rc.10"),
        ("1.0.0-alpha", "1.0.0-alpha.1"),
        ("1.0.0-alpha.1", "1.0.0-alpha.beta"),
        ("1.0.0-beta.11", "1.0.0-rc.1"),
        ("1.9.0", "1.10.0"),
    ],
)
def test_precedence_follows_semver_2_0_0(lower: str, higher: str) -> None:
    assert compare_semver(parts(lower), parts(higher)) == -1
    assert compare_semver(parts(higher), parts(lower)) == 1


_NUM = st.integers(min_value=0, max_value=50)
_IDENT = st.one_of(
    st.integers(min_value=0, max_value=20).map(str),
    st.sampled_from(["alpha", "beta", "rc", "x-y"]),
)


@st.composite
def versions(draw: st.DrawFn) -> str:
    core = f"{draw(_NUM)}.{draw(_NUM)}.{draw(_NUM)}"
    pre = draw(st.lists(_IDENT, max_size=3))
    build = draw(st.lists(st.sampled_from(["a", "b", "7"]), max_size=2))
    text = core
    if pre:
        text += "-" + ".".join(pre)
    if build:
        text += "+" + ".".join(build)
    return text


@given(candidate=versions(), existing=st.lists(versions(), max_size=6))
def test_validate_publish_accepts_exactly_a_version_greater_than_every_existing_one(
    candidate: str, existing: list[str]
) -> None:
    rows = [parts(text) for text in existing]
    declared = parts(candidate)
    greater = all(compare_semver(declared, row) > 0 for row in rows)
    if greater:
        assert validate_publish(candidate, rows) == declared
        return
    with pytest.raises(ProblemError) as raised:
        validate_publish(candidate, rows)
    problem = raised.value
    assert problem.status == 409
    assert problem.slug == "semver-conflict"
    highest = rows[0]
    for row in rows[1:]:
        if compare_semver(row, highest) > 0:
            highest = row
    assert problem.extensions["highest_semver"] == highest.text
    equal = [row for row in rows if compare_semver(declared, row) == 0]
    if equal:
        assert problem.extensions["existing_semver"] == equal[0].text
    else:
        assert "existing_semver" not in problem.extensions


@given(
    bad=st.one_of(
        st.none(),
        st.integers(),
        st.floats(allow_nan=False),
        st.lists(st.text(), max_size=2),
        st.text(max_size=8).filter(lambda t: parse_semver(t) is None),
    ),
    existing=st.lists(versions(), max_size=3),
)
def test_a_declared_version_that_is_not_a_strict_semver_string_is_422(
    bad: object, existing: list[str]
) -> None:
    with pytest.raises(ProblemError) as raised:
        validate_publish(bad, [parts(text) for text in existing])
    assert raised.value.status == 422
    assert raised.value.slug == "invalid-version"


def test_bump_increments_resets_lower_parts_and_drops_prerelease_and_build() -> None:
    assert bump("1.2.0-rc.1", "minor") == "1.3.0"
    assert bump("1.2.3+build", "major") == "2.0.0"
    assert bump("1.2.3", "minor") == "1.3.0"
    assert bump("1.2.3", "patch") == "1.2.4"


def test_bump_leaves_unusable_input_unchanged_and_none_is_verbatim() -> None:
    assert bump("not-semver", "minor") == "not-semver"
    assert bump(None, "minor") is None
    assert bump("1.2.3", "none") == "1.2.3"
    assert bump("v1", "none") == "v1"
    assert bump(f"{MAX_SEMVER_PART}.0.0", "major") == f"{MAX_SEMVER_PART}.0.0"
