"""Tracer: raw ODCS bytes -> strictly parsed, gated, structurally reported contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from dc_odcs import (
    extract_search_fields,
    parse_contract,
    validate_structure,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def test_v320_yaml_goes_from_raw_bytes_to_gated_and_structurally_valid_document() -> None:
    raw = (FIXTURES / "valid-v3.2.0.yaml").read_bytes()

    parsed = parse_contract(raw, "application/yaml")

    assert parsed.api_version == "v3.2.0"
    assert parsed.odcs_id == "urn:datacontract:payments:orders"
    assert parsed.kind == "DataContract"
    assert parsed.media_type == "application/yaml"
    assert parsed.size_bytes == len(raw)
    assert parsed.content_sha256 == hashlib.sha256(raw).hexdigest()
    # The parse never retypes values: the timestamp-looking value stays a string.
    assert parsed.document["customProperties"] == [
        {"property": "costCentre", "value": "cc-4711"},
        {"property": "reviewedOn", "value": "2026-11-09T10:00:00Z"},
    ]
    report = validate_structure(parsed.document, parsed.api_version)
    assert report.valid is True
    assert report.error_count == 0
    assert report.errors == ()


def test_v310_json_is_gated_and_validated_against_the_v310_schema() -> None:
    raw = (FIXTURES / "valid-v3.1.0.json").read_bytes()

    parsed = parse_contract(raw, "application/json")

    assert parsed.api_version == "v3.1.0"
    assert validate_structure(parsed.document, parsed.api_version).valid is True

    # The 3.1.0 schema requires `status`; the same document without it must be reported.
    without_status = {k: v for k, v in parsed.document.items() if k != "status"}
    report = validate_structure(without_status, "v3.1.0")
    assert report.valid is False
    assert any("status" in error.message for error in report.errors)


def test_structural_error_is_reported_not_raised() -> None:
    raw = json.dumps(
        {
            "apiVersion": "v3.2.0",
            "kind": "DataContract",
            "id": "urn:x",
            "version": "1.0.0",
            "unknownTop": 1,
        }
    ).encode()

    parsed = parse_contract(raw, "application/json")  # gate passes

    report = validate_structure(parsed.document, parsed.api_version)
    assert report.valid is False
    assert report.error_count >= 1
    assert "unknownTop" in report.errors[0].message


def test_extract_search_fields_reads_the_fixture() -> None:
    raw = (FIXTURES / "valid-v3.2.0.yaml").read_bytes()
    document = parse_contract(raw, "application/yaml").document

    fields = extract_search_fields(document)

    assert fields.name == "Payments Orders"
    assert fields.domain == "payments"
    assert fields.status == "draft"
    assert fields.tags == ("payments", "orders", "pii-free")
    assert fields.odcs_version == "1.0.0"
    assert fields.description_text == (
        "Records every order placed through the checkout.\n"
        "Reporting and reconciliation.\n"
        "Orders older than seven years are archived."
    )


@pytest.mark.parametrize("description", ["free text", None, {"purpose": "", "usage": 3}])
def test_a_description_without_text_parts_gives_no_description_text(description: object) -> None:
    fields = extract_search_fields({"name": "orders", "description": description, "tags": "x"})
    assert fields.name == "orders"
    assert fields.description_text is None
    assert fields.tags == ()
