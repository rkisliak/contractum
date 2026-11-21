"""Gate matrix: media types, apiVersion/kind/id allow-list, edge truths (empty, encoding)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from dc_odcs import (
    SUPPORTED_API_VERSIONS,
    ContractRejected,
    gate,
    parse_contract,
    validate_structure,
)

pytestmark = pytest.mark.unit

JSON = "application/json"
YAML = "application/yaml"


def _doc(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "apiVersion": "v3.2.0",
        "kind": "DataContract",
        "id": "urn:datacontract:x",
        "version": "1.0.0",
    }
    for key, value in overrides.items():
        if value is _MISSING:
            doc.pop(key, None)
        else:
            doc[key] = value
    return doc


_MISSING = object()


JSON_HEAD = b'{"apiVersion": "v3.2.0", "kind": "DataContract", "id": "x"'


def _raw(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc).encode()


def _reject(raw: bytes, media_type: str = JSON, **kwargs: Any) -> ContractRejected:
    with pytest.raises(ContractRejected) as excinfo:
        parse_contract(raw, media_type, **kwargs)
    return excinfo.value


def test_supported_set_is_exactly_the_two_explicit_versions() -> None:
    assert SUPPORTED_API_VERSIONS == frozenset({"v3.1.0", "v3.2.0"})


@pytest.mark.parametrize("version", ["v3.1.0", "v3.2.0"])
def test_supported_versions_pass_the_gate(version: str) -> None:
    parsed = parse_contract(_raw(_doc(apiVersion=version)), JSON)
    assert parsed.api_version == version


@pytest.mark.parametrize(
    "api_version",
    ["v3.0.2", "v2.2.2", "v3.1.1", "v3.3.0", _MISSING, 3.2, None],
    ids=["v3.0.2", "v2.2.2", "v3.1.1", "v3.3.0", "missing", "number", "null"],
)
def test_unsupported_api_version_is_rejected_naming_both_supported_versions(
    api_version: Any,
) -> None:
    rejected = _reject(_raw(_doc(apiVersion=api_version)))
    assert rejected.status == 422
    assert rejected.slug == "unsupported-odcs-version"
    assert rejected.pointer == "/apiVersion"
    assert "Supported ODCS versions: v3.1.0, v3.2.0" in rejected.detail


def test_prefix_and_range_forms_are_not_accepted() -> None:
    for candidate in ("v3.1", "v3.1.x", "3.2.0", "V3.2.0", " v3.2.0", "v3.2.0 "):
        assert _reject(_raw(_doc(apiVersion=candidate))).slug == "unsupported-odcs-version"


def test_supported_argument_narrows_the_allow_list() -> None:
    rejected = _reject(_raw(_doc(apiVersion="v3.1.0")), supported=frozenset({"v3.2.0"}))
    assert rejected.slug == "unsupported-odcs-version"
    assert rejected.detail.endswith("Supported ODCS versions: v3.2.0")


def test_oversized_api_version_value_is_not_echoed_in_full() -> None:
    rejected = _reject(_raw(_doc(apiVersion="v" * 5000)))
    assert len(rejected.detail) < 200


@pytest.mark.parametrize("kind", ["Other", "datacontract", None, 1, _MISSING])
def test_kind_must_be_data_contract(kind: Any) -> None:
    rejected = _reject(_raw(_doc(kind=kind)))
    assert (rejected.status, rejected.slug, rejected.pointer) == (
        422,
        "validation-error",
        "/kind",
    )


@pytest.mark.parametrize("odcs_id", ["", "x" * 256, 123, None, ["a"], _MISSING])
def test_id_must_be_a_non_empty_string_of_at_most_255_chars(odcs_id: Any) -> None:
    rejected = _reject(_raw(_doc(id=odcs_id)))
    assert (rejected.status, rejected.slug, rejected.pointer) == (
        422,
        "validation-error",
        "/id",
    )


def test_the_public_bound_is_255_and_the_gate_uses_it() -> None:
    from dc_odcs import MAX_ODCS_ID_LENGTH

    assert MAX_ODCS_ID_LENGTH == 255
    assert parse_contract(_raw(_doc(id="x" * MAX_ODCS_ID_LENGTH)), JSON).odcs_id
    assert _reject(_raw(_doc(id="x" * (MAX_ODCS_ID_LENGTH + 1)))).pointer == "/id"


def test_id_of_exactly_255_chars_is_accepted() -> None:
    assert parse_contract(_raw(_doc(id="x" * 255)), JSON).odcs_id == "x" * 255


def test_gate_order_is_api_version_then_kind_then_id() -> None:
    both_bad = _doc(apiVersion="v9", kind="Nope", id="")
    assert _reject(_raw(both_bad)).pointer == "/apiVersion"
    assert _reject(_raw(_doc(kind="Nope", id=""))).pointer == "/kind"


class TestMediaTypes:
    def test_parameters_and_case_are_ignored(self) -> None:
        parsed = parse_contract(
            b"apiVersion: v3.2.0\nkind: DataContract\nid: x\n", "Application/YAML; charset=utf-8"
        )
        assert parsed.media_type == "application/yaml"

    @pytest.mark.parametrize("media_type", ["application/x-yaml", "text/yaml"])
    def test_yaml_aliases_are_accepted(self, media_type: str) -> None:
        raw = b"apiVersion: v3.2.0\nkind: DataContract\nid: x\n"
        assert parse_contract(raw, media_type).api_version == "v3.2.0"

    @pytest.mark.parametrize("media_type", ["text/plain", "application/xml", "", "yaml"])
    def test_other_media_types_are_415(self, media_type: str) -> None:
        rejected = _reject(_raw(_doc()), media_type)
        assert (rejected.status, rejected.slug) == (415, "unsupported-media-type")

    def test_media_type_is_checked_before_the_body(self) -> None:
        assert _reject(b"", "text/plain").status == 415


class TestEmptyAndRootShape:
    def test_empty_body_is_malformed_document(self) -> None:
        for media_type in (JSON, YAML):
            rejected = _reject(b"", media_type)
            assert (rejected.status, rejected.slug) == (422, "malformed-document")

    def test_empty_mapping_is_unsupported_odcs_version(self) -> None:
        for raw, media_type in ((b"{}", JSON), (b"{}", YAML)):
            rejected = _reject(raw, media_type)
            assert (rejected.status, rejected.slug) == (422, "unsupported-odcs-version")

    @pytest.mark.parametrize(
        ("raw", "media_type"),
        [
            (b"null", JSON),
            (b"42", JSON),
            (b'"text"', JSON),
            (b"[]", JSON),
            (b"[1, 2]", JSON),
            (b"null", YAML),
            (b"~", YAML),
            (b"just a scalar", YAML),
            (b"- a\n- b\n", YAML),
            (b"   \n", YAML),
            (b"# only a comment\n", YAML),
        ],
    )
    def test_empty_or_non_mapping_root_is_malformed_document(
        self, raw: bytes, media_type: str
    ) -> None:
        rejected = _reject(raw, media_type)
        assert (rejected.status, rejected.slug) == (422, "malformed-document")
        assert "document root must be a mapping" in rejected.detail


class TestEncoding:
    def test_encoding_invalid_utf8_is_422(self) -> None:
        rejected = _reject(b'{"apiVersion": "\xff\xfe"}')
        assert (rejected.status, rejected.slug) == (422, "malformed-document")
        assert "UTF-8" in rejected.detail

    @pytest.mark.parametrize("media_type", [JSON, YAML])
    def test_encoding_utf16_with_bom_is_422(self, media_type: str) -> None:
        raw = json.dumps(_doc()).encode("utf-16")
        assert raw[:2] in (b"\xff\xfe", b"\xfe\xff")
        assert _reject(raw, media_type).status == 422

    @pytest.mark.parametrize("media_type", [JSON, YAML])
    def test_encoding_utf16_without_bom_is_422(self, media_type: str) -> None:
        assert _reject(json.dumps(_doc()).encode("utf-16-le"), media_type).status == 422

    @pytest.mark.parametrize("media_type", [JSON, YAML])
    def test_encoding_utf8_bom_is_ignored_for_parsing_but_hashed(self, media_type: str) -> None:
        raw = b"\xef\xbb\xbf" + json.dumps(_doc()).encode()
        parsed = parse_contract(raw, media_type)
        assert parsed.api_version == "v3.2.0"
        assert parsed.content_sha256 == hashlib.sha256(raw).hexdigest()
        assert parsed.content_sha256 != hashlib.sha256(raw[3:]).hexdigest()
        assert parsed.size_bytes == len(raw)

    def test_encoding_only_one_leading_bom_is_stripped(self) -> None:
        raw = b"\xef\xbb\xbf\xef\xbb\xbf" + json.dumps(_doc()).encode()
        assert _reject(raw).slug == "malformed-document"

    def test_encoding_nul_in_nested_json_value_is_422_with_pointer(self) -> None:
        raw = _raw(_doc(schema=[{"name": "t", "description": "a\u0000b"}]))
        rejected = _reject(raw)
        assert (rejected.status, rejected.slug) == (422, "malformed-document")
        assert rejected.pointer == "/schema/0/description"

    def test_encoding_nul_in_key_is_422_with_escaped_pointer(self) -> None:
        rejected = _reject(_raw(_doc(**{"a/b\u0000": 1})))
        assert rejected.slug == "malformed-document"
        assert rejected.pointer == "/a~1b\u0000"

    def test_encoding_nul_in_yaml_escape_is_422(self) -> None:
        raw = b'apiVersion: v3.2.0\nkind: DataContract\nid: x\nnote: "a\\0b"\n'
        rejected = _reject(raw, YAML)
        assert rejected.pointer == "/note"

    def test_encoding_lone_surrogate_in_json_value_is_422_with_pointer(self) -> None:
        rejected = _reject(JSON_HEAD + b', "name": "a\\ud800b"}')
        assert (rejected.status, rejected.slug) == (422, "malformed-document")
        assert rejected.pointer == "/name"
        assert "surrogate" in rejected.detail

    def test_encoding_lone_surrogate_in_nested_value_and_key_is_422(self) -> None:
        nested = _reject(_raw(_doc(schema=[{"name": "t", "description": "x\ude00"}])))
        assert nested.pointer == "/schema/0/description"
        key = _reject(
            b'{"apiVersion": "v3.2.0", "kind": "DataContract", "id": "x", "a/\\udc00": 1}'
        )
        assert key.slug == "malformed-document"
        assert key.pointer == "/a~1\udc00"

    def test_encoding_lone_surrogate_in_yaml_escape_is_422(self) -> None:
        raw = b'apiVersion: v3.2.0\nkind: DataContract\nid: x\nnote: "a\\uD800b"\n'
        rejected = _reject(raw, YAML)  # libyaml refuses the escape itself
        assert (rejected.status, rejected.slug) == (422, "malformed-document")

    def test_encoding_valid_surrogate_pair_survives(self) -> None:
        raw = (
            b'{"apiVersion": "v3.2.0", "kind": "DataContract", "id": "x", "name": "\\ud83d\\ude00"}'
        )
        assert parse_contract(raw, JSON).document["name"] == "\U0001f600"

    def test_encoding_raw_nul_byte_is_422(self) -> None:
        assert _reject(b'{"apiVersion": "v3.2.0\x00"}').status == 422

    def test_encoding_non_ascii_text_survives(self) -> None:
        parsed = parse_contract(_raw(_doc(name="Zażółć gęślą jaźń 日本語")), JSON)
        assert parsed.document["name"] == "Zażółć gęślą jaźń 日本語"


class TestSizeCap:
    def test_size_is_measured_in_raw_bytes_at_the_boundary(self) -> None:
        raw = _raw(_doc())
        parsed = parse_contract(raw, JSON, max_bytes=len(raw))
        assert parsed.size_bytes == len(raw)
        rejected = _reject(raw, max_bytes=len(raw) - 1)
        assert (rejected.status, rejected.slug) == (413, "payload-too-large")

    def test_multibyte_characters_count_as_bytes_not_characters(self) -> None:
        raw = json.dumps(_doc(name="日本語" * 10), ensure_ascii=False).encode()
        assert len(raw.decode()) < len(raw)
        assert _reject(raw, max_bytes=len(raw.decode())).status == 413

    def test_no_cap_means_no_413(self) -> None:
        assert parse_contract(_raw(_doc(name="x" * 100_000)), JSON).size_bytes > 100_000

    def test_size_cap_is_checked_before_parsing(self) -> None:
        assert _reject(b"{" * 1000, max_bytes=10).status == 413


class TestNonBlockingStructuralReport:
    def test_unquoted_float_version_gates_but_is_reported_at_pointer_version(self) -> None:
        raw = b"apiVersion: v3.2.0\nkind: DataContract\nid: urn:x\nversion: 1.0\n"
        parsed = parse_contract(raw, YAML)
        assert parsed.document["version"] == 1.0
        report = validate_structure(parsed.document, parsed.api_version)
        assert report.valid is False
        assert "/version" in {error.pointer for error in report.errors}

    def test_report_is_capped_at_50_errors_but_counts_all(self) -> None:
        document = _doc(tags=list(range(80)))
        report = validate_structure(document, "v3.2.0")
        assert report.valid is False
        assert len(report.errors) == 50
        assert report.error_count == 80
        assert [error.pointer for error in report.errors] == [f"/tags/{i}" for i in range(50)]

    def test_pointer_escapes_slash_and_tilde(self) -> None:
        document = _doc(customProperties=[{"property": "a/b~c", "value": {"x": 1}, "bad": 1}])
        report = validate_structure(document, "v3.2.0")
        assert report.valid is False
        assert all(error.pointer.startswith("/customProperties/0") for error in report.errors)

    def test_unknown_api_version_for_validation_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="No vendored ODCS schema"):
            validate_structure(_doc(), "v3.0.2")


@pytest.mark.parametrize(("media_type", "label"), [(JSON, "JSON"), (YAML, "YAML")])
def test_a_parser_recursion_error_is_a_malformed_document(
    monkeypatch: pytest.MonkeyPatch, media_type: str, label: str
) -> None:
    def overflow(_text: str) -> Any:
        raise RecursionError

    monkeypatch.setattr(gate, "load_json", overflow)
    monkeypatch.setattr(gate, "load_yaml", overflow)
    rejected = _reject(_raw(_doc()), media_type)
    assert (rejected.status, rejected.slug) == (422, "malformed-document")
    assert rejected.detail == f"Invalid {label}: nesting is too deep"
