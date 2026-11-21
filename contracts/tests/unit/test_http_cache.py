"""ETag and If-Match evaluation (D-42): strong comparison, lists, ``*``, malformed headers."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from dc_contracts.http_cache import (
    deprecation_headers,
    etag_for_draft,
    if_match_ok,
    if_none_match_hit,
    parse_etag_list,
    require_if_match,
    version_etag,
)
from dc_core.problems import ProblemError
from hypothesis import given
from hypothesis import strategies as st

pytestmark = pytest.mark.unit

revisions = st.integers(min_value=1, max_value=2_147_483_647)


def test_etag_for_draft_is_the_quoted_strong_revision_tag() -> None:
    assert etag_for_draft(1) == '"r1"'
    assert etag_for_draft(7, "json") == '"r7-json"'
    assert etag_for_draft(7, "yaml") == '"r7-yaml"'


@given(revisions)
def test_the_current_etag_matches_itself_and_a_weak_copy_never_does(revision: int) -> None:
    current = etag_for_draft(revision)
    assert if_match_ok(current, current) is True
    assert if_match_ok("W/" + current, current) is False


@given(revisions, revisions)
def test_a_different_revision_never_matches(a: int, b: int) -> None:
    if a != b:
        assert if_match_ok(etag_for_draft(a), etag_for_draft(b)) is False


def test_a_list_matches_when_any_strong_member_is_current() -> None:
    assert if_match_ok('"a", "r3"', '"r3"') is True
    assert if_match_ok('"a" ,"r3"  ', '"r3"') is True
    assert if_match_ok('W/"r3", "a"', '"r3"') is False


def test_star_matches_an_existing_resource() -> None:
    assert parse_etag_list("*") == "*"
    assert parse_etag_list("  *  ") == "*"
    assert if_match_ok("*", '"r9"') is True


@pytest.mark.parametrize(
    "header",
    ["r1", '"r1', 'r1"', '"r1",', ',"r1"', '"a",,"b"', '""x""', '"a b"', "", "   ", "* , *", "**"],
)
def test_malformed_headers_match_nothing(header: str) -> None:
    assert parse_etag_list(header) is None
    assert if_match_ok(header, '"r1"') is False


def test_no_header_matches_nothing() -> None:
    assert if_match_ok(None, '"r1"') is False


@pytest.mark.parametrize("header", [None, "", "   "])
def test_require_if_match_is_428_without_a_header(header: str | None) -> None:
    with pytest.raises(ProblemError) as caught:
        require_if_match(header, '"r1"')
    assert caught.value.status == 428
    assert caught.value.slug == "precondition-required"


@pytest.mark.parametrize("header", ['"x"', 'W/"r1"', "garbage"])
def test_require_if_match_is_412_for_a_stale_weak_or_malformed_tag(header: str) -> None:
    with pytest.raises(ProblemError) as caught:
        require_if_match(header, '"r1"')
    assert caught.value.status == 412
    assert caught.value.slug == "precondition-failed"
    assert header not in (caught.value.detail or "")


def test_require_if_match_passes_for_the_current_tag_and_star() -> None:
    require_if_match('"r1"', '"r1"')
    require_if_match("*", '"r1"')


# ---- If-None-Match (RFC 9110 section 13.1.2): weak comparison, lists and * ----


@pytest.mark.parametrize(
    ("header", "current", "expected"),
    [
        ('"a"', '"a"', True),
        ('W/"a"', '"a"', True),
        ('"a"', 'W/"a"', True),
        ('"x", "a"', '"a"', True),
        ('"a", "b"', '"b"', True),
        ('W/"x" ,  W/"a"', '"a"', True),
        ("*", '"anything"', True),
        ("  *  ", '"anything"', True),
        ('"a"', '"b"', False),
        ('"a-json"', '"a"', False),
        ('"a"', '"a-json"', False),
        ('"a" "b"', '"a"', False),
        ('"a",', '"a"', False),
        ("a", '"a"', False),
        ('"a', '"a"', False),
        ("* , *", '"a"', False),
        ("", '"a"', False),
        ("   ", '"a"', False),
        (None, '"a"', False),
    ],
)
def test_if_none_match_hit_uses_weak_comparison_lists_and_star(
    header: str | None, current: str, expected: bool
) -> None:
    assert if_none_match_hit(header, current) is expected


@given(st.text(max_size=40))
def test_if_none_match_hit_never_raises_and_a_garbled_header_is_a_miss(header: str) -> None:
    result = if_none_match_hit(header, '"abc"')
    assert isinstance(result, bool)


@given(st.text(alphabet="0123456789abcdef", min_size=1, max_size=64))
def test_a_version_etag_hits_itself_and_never_the_other_representations(sha: str) -> None:
    original = version_etag(sha)
    assert if_none_match_hit(original, original) is True
    assert if_none_match_hit("W/" + original, original) is True
    for target in ("json", "yaml"):
        other = version_etag(sha, target)
        assert if_none_match_hit(original, other) is False
        assert if_none_match_hit(other, original) is False


def test_deprecation_headers_are_an_rfc_9745_date_and_an_rfc_8594_http_date() -> None:
    headers = deprecation_headers(datetime(2024, 6, 1, 12, 0, tzinfo=UTC), date(2024, 6, 30))
    assert headers == {"Deprecation": "@1717243200", "Sunset": "Sun, 30 Jun 2024 23:59:59 GMT"}


def test_deprecation_headers_use_whole_seconds_and_the_end_of_the_sunset_day_in_utc() -> None:
    headers = deprecation_headers(
        datetime(2024, 6, 1, 12, 0, 0, 999_999, tzinfo=UTC), date(2025, 1, 1)
    )
    assert headers["Deprecation"] == "@1717243200"
    assert headers["Sunset"] == "Wed, 01 Jan 2025 23:59:59 GMT"
    assert set(headers) == {"Deprecation", "Sunset"}
