"""Structural report size is bounded, whatever the submitted values are (WR-03)."""

from __future__ import annotations

import pytest
from dc_odcs import validate_structure
from dc_odcs.validate import MAX_MESSAGE_CHARS, MAX_REPORTED_ERRORS

pytestmark = pytest.mark.unit


def test_a_huge_offending_value_cannot_inflate_the_report() -> None:
    document = {"version": "x" * 400_000, "kind": "x" * 400_000, "apiVersion": "v3.2.0"}

    report = validate_structure(document, "v3.2.0")

    assert report.valid is False
    assert report.error_count >= 2
    assert len(report.errors) <= MAX_REPORTED_ERRORS
    assert all(len(error.message) <= MAX_MESSAGE_CHARS for error in report.errors)
    assert sum(len(error.message) for error in report.errors) < 50 * MAX_MESSAGE_CHARS


def test_short_messages_are_left_untouched() -> None:
    report = validate_structure({"apiVersion": "v3.2.0"}, "v3.2.0")
    assert report.valid is False
    assert report.errors
    assert not any(error.message.endswith("...") for error in report.errors)


def _base() -> dict[str, object]:
    return {"apiVersion": "v3.2.0", "kind": "DataContract", "id": "c-1", "version": "1.0.0"}


def test_a_wrong_typed_field_reports_the_type_keyword() -> None:
    report = validate_structure({**_base(), "id": 5}, "v3.2.0")

    assert [(e.pointer, e.keyword) for e in report.errors] == [("/id", "type")]


def test_a_missing_required_field_reports_the_required_keyword() -> None:
    document = _base()
    del document["version"]

    report = validate_structure(document, "v3.2.0")

    assert [e.keyword for e in report.errors] == ["required"]


def test_an_unknown_top_level_key_reports_the_property_restriction_keyword() -> None:
    report = validate_structure({**_base(), "zz": 1}, "v3.2.0")

    assert [(e.pointer, e.keyword) for e in report.errors] == [("", "additionalProperties")]


def test_a_structural_error_built_without_a_keyword_defaults_to_schema() -> None:
    from dc_odcs import StructuralError

    assert StructuralError(pointer="/a", message="m").keyword == "schema"


@pytest.mark.parametrize(
    ("schema_path", "expected"),
    [
        (["properties", "id", "type"], "type"),
        (["allOf", 1], "allOf"),
        ([0, 1], "schema"),
        ([], "schema"),
    ],
)
def test_the_keyword_is_the_last_named_schema_path_segment(
    schema_path: list[object], expected: str
) -> None:
    from dc_odcs.validate import _keyword

    assert _keyword(schema_path) == expected
