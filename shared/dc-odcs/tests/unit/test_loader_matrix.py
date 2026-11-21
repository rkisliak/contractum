"""Strict loader matrix: YAML 1.1 implicit typing, duplicate keys, aliases, JSON oddities."""

from __future__ import annotations

import json

import pytest
import yaml
from dc_odcs import ContractRejected, load_json, load_yaml, parse_contract

pytestmark = pytest.mark.unit

BASE = "apiVersion: v3.2.0\nkind: DataContract\nid: urn:x\n"


def _reject(body: str, media_type: str = "application/yaml") -> ContractRejected:
    with pytest.raises(ContractRejected) as excinfo:
        parse_contract(body.encode(), media_type)
    return excinfo.value


class TestYamlTyping:
    def test_norway_problem_stays_a_string(self) -> None:
        assert load_yaml("country: NO") == {"country": "NO"}

    def test_yaml_11_booleans_stay_strings(self) -> None:
        assert load_yaml("on: yes") == {"on": "yes"}
        assert load_yaml("a: off\nb: No\nc: y") == {"a": "off", "b": "No", "c": "y"}

    def test_only_lowercase_true_and_false_are_booleans(self) -> None:
        assert load_yaml("a: true\nb: false") == {"a": True, "b": False}

    def test_timestamps_stay_strings(self) -> None:
        assert load_yaml("ts: 2026-11-09T10:00:00Z") == {"ts": "2026-11-09T10:00:00Z"}
        assert load_yaml("d: 2026-11-09") == {"d": "2026-11-09"}

    def test_loading_does_not_leak_resolver_changes_into_the_stock_safe_loader(self) -> None:
        load_yaml("a: 1")
        assert yaml.load("a: NO", Loader=yaml.CSafeLoader) == {"a": False}  # noqa: S506

    def test_unquoted_float_version_parses_as_float(self) -> None:
        assert load_yaml("version: 1.0") == {"version": 1.0}


class TestYamlNumbers:
    """WR-08: YAML 1.1 numeric look-alikes stay strings; plain decimals are numbers."""

    @pytest.mark.parametrize(
        "text",
        [
            "0755",  # YAML 1.1 octal
            "010",
            "00",
            "0b11",
            "0x1F",
            "0o17",
            "1_000",
            "12:30",  # base-60
            "1:30:15",
            "1_0.5",
            "1:30.5",
            "1,000",
        ],
    )
    def test_look_alikes_stay_strings(self, text: str) -> None:
        assert load_yaml(f"v: {text}") == {"v": text}

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("0", 0), ("42", 42), ("-5", -5), ("+1", 1), ("-0", 0), ("1.5", 1.5), (".5", 0.5)],
    )
    def test_plain_decimals_are_numbers(self, text: str, expected: float) -> None:
        loaded = load_yaml(f"v: {text}")["v"]
        assert loaded == expected
        assert type(loaded) is type(expected)

    def test_signed_exponent_floats_and_inf_are_floats(self) -> None:
        assert load_yaml("v: -2.5e-3") == {"v": -0.0025}
        assert load_yaml("v: .inf") == {"v": float("inf")}

    def test_quoted_numbers_stay_strings(self) -> None:
        assert load_yaml("v: '42'") == {"v": "42"}

    def test_a_version_like_value_is_not_retyped_in_a_contract(self) -> None:
        parsed = parse_contract((BASE + "version: 010\n").encode(), "application/yaml")
        assert parsed.document["version"] == "010"


class TestYamlRejections:
    def test_duplicate_key_is_rejected_naming_the_key(self) -> None:
        rejected = _reject(BASE + "a: 1\na: 2\n")
        assert (rejected.status, rejected.slug) == (422, "malformed-document")
        assert "duplicate key 'a'" in rejected.detail
        assert "line" in rejected.detail

    def test_nested_duplicate_key_is_rejected(self) -> None:
        assert "duplicate key 'k'" in _reject(BASE + "m:\n  k: 1\n  k: 2\n").detail

    def test_unhashable_complex_key_is_rejected_with_its_position(self) -> None:
        rejected = _reject(BASE + "? [a, b]\n: 1\n")
        assert (rejected.status, rejected.slug) == (422, "malformed-document")
        assert rejected.detail == "Invalid YAML: found unhashable key (line 4, column 3)"

    def test_alias_is_rejected(self) -> None:
        rejected = _reject(BASE + "x: &a 1\ny: *a\n")
        assert (rejected.status, rejected.slug) == (422, "malformed-document")
        assert "YAML aliases are not allowed" in rejected.detail

    def test_anchor_without_alias_is_rejected(self) -> None:
        assert "YAML aliases are not allowed" in _reject(BASE + "x: &a 1\n").detail

    def test_billion_laughs_is_rejected_before_expansion(self) -> None:
        bomb = "a: &a [x, x, x, x, x, x, x, x, x]\n" + "".join(
            f"{n}: &{n} [*{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}]\n"
            for n, p in (("b", "a"), ("c", "b"), ("d", "c"), ("e", "d"), ("f", "e"))
        )
        assert "YAML aliases are not allowed" in _reject(BASE + bomb).detail

    def test_merge_key_is_rejected(self) -> None:
        assert "merge keys" in _reject(BASE + "<<: {a: 1}\n").detail

    @pytest.mark.parametrize(
        ("snippet", "type_name"),
        [("x: !!binary aGVsbG8=\n", "bytes"), ("x: !!set {a, b}\n", "set")],
        ids=["binary-tag", "set-tag"],
    )
    def test_non_json_yaml_tags_are_rejected_with_a_pointer(
        self, snippet: str, type_name: str
    ) -> None:
        rejected = _reject(BASE + snippet)
        assert rejected.slug == "malformed-document"
        assert rejected.pointer == "/x"
        assert type_name in rejected.detail

    def test_explicit_timestamp_tag_is_rejected(self) -> None:
        assert _reject(BASE + "x: !!timestamp 2026-11-09\n").pointer == "/x"

    def test_python_object_tag_is_rejected_by_the_safe_loader(self) -> None:
        assert _reject(BASE + "x: !!python/object:os.system x\n").slug == "malformed-document"

    def test_non_string_mapping_key_is_rejected(self) -> None:
        assert "keys must be strings" in _reject(BASE + "1: a\n").detail

    def test_non_finite_yaml_number_is_rejected(self) -> None:
        rejected = _reject(BASE + "x: .nan\n")
        assert rejected.pointer == "/x"

    def test_syntax_error_reports_line_and_column(self) -> None:
        rejected = _reject(BASE + "x: [\n")
        assert rejected.slug == "malformed-document"
        assert "line" in rejected.detail
        assert "column" in rejected.detail

    def test_yaml_error_detail_never_echoes_document_content(self) -> None:
        rejected = _reject(BASE + "secret_value_marker: [\n")
        assert "secret_value_marker" not in rejected.detail

    def test_extremely_deep_nesting_is_a_422_not_a_crash(self) -> None:
        assert _reject("[" * 100_000).slug == "malformed-document"


class TestJsonStrictness:
    def test_duplicate_json_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate key 'a'"):
            load_json('{"a": 1, "a": 2}')
        rejected = _reject('{"a": 1, "a": 2}', "application/json")
        assert (rejected.status, rejected.slug) == (422, "malformed-document")
        assert "duplicate key 'a'" in rejected.detail

    @pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
    def test_non_standard_json_constants_are_rejected(self, constant: str) -> None:
        rejected = _reject('{"a": ' + constant + "}", "application/json")
        assert (rejected.status, rejected.slug) == (422, "malformed-document")

    def test_json_syntax_error_reports_position(self) -> None:
        rejected = _reject('{"a": ', "application/json")
        assert "line 1, column" in rejected.detail

    def test_deep_json_nesting_is_a_422_not_a_crash(self) -> None:
        assert _reject("[" * 100_000, "application/json").slug == "malformed-document"

    def test_json_content_is_not_retyped(self) -> None:
        text = json.dumps({"a": "NO", "b": "2026-11-09T10:00:00Z", "c": 1.5})
        assert load_json(text) == {"a": "NO", "b": "2026-11-09T10:00:00Z", "c": 1.5}
