"""If-Match evaluation shared by the services (D-42, RFC 9110 sections 8.8.3 and 13.1.1)."""

from __future__ import annotations

import pytest
from dc_core.conditional import (
    if_match_ok,
    parse_etag_list,
    precondition_required,
    require_if_match,
)
from dc_core.problems import ProblemError

pytestmark = pytest.mark.unit


def _raised(header: str | None, current: str) -> ProblemError:
    with pytest.raises(ProblemError) as caught:
        require_if_match(header, current)
    return caught.value


def test_a_missing_if_match_is_428_precondition_required() -> None:
    problem = _raised(None, '"r1"')
    assert (problem.status, problem.slug) == (428, "precondition-required")


def test_a_blank_if_match_is_428_precondition_required() -> None:
    assert _raised("   ", '"r1"').status == 428


def test_a_stale_if_match_is_412_and_never_echoes_the_header() -> None:
    problem = _raised('"r0"', '"r1"')
    assert (problem.status, problem.slug) == (412, "precondition-failed")
    assert "r0" not in (problem.detail or "")
    assert "r0" not in str(problem)


def test_a_matching_if_match_passes_silently() -> None:
    assert require_if_match('"r1"', '"r1"') is None


def test_star_matches_an_existing_resource() -> None:
    assert if_match_ok("*", '"r1"') is True
    assert require_if_match("*", '"r1"') is None


def test_any_element_of_a_list_may_match() -> None:
    assert if_match_ok('"r0", "r1"', '"r1"') is True


def test_a_weak_tag_never_matches_under_the_strong_comparison() -> None:
    assert if_match_ok('W/"r1"', '"r1"') is False
    assert _raised('W/"r1"', '"r1"').status == 412


@pytest.mark.parametrize("header", ["r1", '"r1', '"r1",', '"r1" "r2"', ",", '""x'])
def test_a_malformed_header_matches_nothing(header: str) -> None:
    assert parse_etag_list(header) is None
    assert if_match_ok(header, '"r1"') is False


def test_an_absent_header_matches_nothing() -> None:
    assert if_match_ok(None, '"r1"') is False


def test_parse_returns_weakness_and_opaque_pairs() -> None:
    assert parse_etag_list('W/"a", "b"') == [(True, "a"), (False, "b")]
    assert parse_etag_list("*") == "*"


def test_precondition_required_is_a_428_problem() -> None:
    problem = precondition_required()
    assert (problem.status, problem.slug) == (428, "precondition-required")
