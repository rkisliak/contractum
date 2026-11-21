"""Rule model: key and message encoding, canonical JSON and content hash (D-109, D-110, D-117).

Encoding edge (RULE-01): the key is ASCII only; the message is limited in Unicode code points
(Python `len`); the condition in UTF-8 bytes (`Limits.max_expression_bytes`); the target in code
points (`Limits.max_target_chars`).
"""

from __future__ import annotations

from typing import Any

import pytest
from dc_rules.model import (
    Fixture,
    FixtureCtx,
    RuleDef,
    RuleSetContent,
    canonical_json,
    content_sha256,
)
from dc_rules.ports import Limits
from pydantic import ValidationError

pytestmark = pytest.mark.unit

FOUR_BYTE = "\U0001f600"  # one code point, four UTF-8 bytes


def rule_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "key": "unique-names",
        "target": "$.schema[*]",
        "condition": "true",
        "severity": "error",
        "message": "Names must be unique.",
        "fixtures": {"pass": [{"document": {}}], "fail": [{"document": {}}]},
    }
    body.update(overrides)
    return body


def content(**rule_overrides: Any) -> RuleSetContent:
    return RuleSetContent.model_validate({"rules": [rule_body(**rule_overrides)]})


# --- key ------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["a", "a-1", "a" * 63])
def test_valid_keys_are_accepted(key: str) -> None:
    assert RuleDef.model_validate(rule_body(key=key)).key == key


@pytest.mark.parametrize(
    "key",
    ["A", "1a", "a_b", "-a", "a" * 64, "étude", "aé", "", "a b"],
    ids=[
        "upper",
        "digit-first",
        "underscore",
        "dash-first",
        "64-chars",
        "nonascii-first",
        "nonascii-inside",
        "empty",
        "space",
    ],
)
def test_invalid_keys_are_refused(key: str) -> None:
    with pytest.raises(ValidationError):
        RuleDef.model_validate(rule_body(key=key))


def test_a_trailing_newline_does_not_sneak_past_the_key_pattern() -> None:
    with pytest.raises(ValidationError):
        RuleDef.model_validate(rule_body(key="abc\n"))


# --- message, condition, target sizes -----------------------------------------------------------


def test_a_500_code_point_message_of_4_byte_characters_is_accepted() -> None:
    message = FOUR_BYTE * 500
    assert len(message.encode()) == 2_000
    assert RuleDef.model_validate(rule_body(message=message)).message == message


def test_a_501_code_point_message_is_refused() -> None:
    with pytest.raises(ValidationError):
        RuleDef.model_validate(rule_body(message=FOUR_BYTE * 501))


def test_an_empty_message_is_refused() -> None:
    with pytest.raises(ValidationError):
        RuleDef.model_validate(rule_body(message=""))


def test_a_condition_of_100001_characters_is_refused_by_the_sanity_cap() -> None:
    with pytest.raises(ValidationError):
        RuleDef.model_validate(rule_body(condition="a" * 100_001))


def test_the_condition_limit_is_counted_in_utf8_bytes_and_the_target_limit_in_code_points() -> None:
    limits = Limits()
    assert limits.max_expression_bytes == 2_048
    assert limits.max_target_chars == 512
    four_byte_condition = FOUR_BYTE * 600  # 600 code points, 2,400 bytes
    assert len(four_byte_condition) < limits.max_expression_bytes
    assert len(four_byte_condition.encode()) > limits.max_expression_bytes
    # The model keeps only the sanity cap: both lengths pass here, the rule check applies Limits.
    RuleDef.model_validate(rule_body(condition=four_byte_condition))


# --- strictness and aliases -----------------------------------------------------------------


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        RuleDef.model_validate(rule_body(on_error="pass"))
    with pytest.raises(ValidationError):
        RuleSetContent.model_validate({"rules": [], "extra": 1})


def test_pass_is_accepted_as_the_alias_of_pass_() -> None:
    rule = RuleDef.model_validate(rule_body())
    assert len(rule.fixtures.pass_) == 1
    dumped = rule.model_dump(by_alias=True)
    assert "pass" in dumped["fixtures"]
    assert "pass_" not in dumped["fixtures"]


def test_dsl_version_defaults_to_cel_2_and_accepts_the_legacy_cel_1() -> None:
    assert content().dsl_version == "cel-2"
    assert RuleSetContent.model_validate({"dsl_version": "cel-1", "rules": []}).dsl_version == (
        "cel-1"
    )
    with pytest.raises(ValidationError):
        RuleSetContent.model_validate({"dsl_version": "cel-3", "rules": []})


# --- canonical JSON and content hash --------------------------------------------------------------


def test_canonical_json_ignores_key_order() -> None:
    first = {"b": 1, "a": {"y": 2, "x": [1, {"d": 1, "c": 2}]}}
    second = {"a": {"x": [1, {"c": 2, "d": 1}], "y": 2}, "b": 1}
    assert canonical_json(first) == canonical_json(second)


def test_canonical_json_is_compact_utf8_without_ascii_escaping() -> None:
    assert canonical_json({"a": "é"}) == '{"a":"é"}'.encode()


def test_the_same_content_in_a_different_field_order_has_the_same_hash() -> None:
    reordered = dict(reversed(list(rule_body().items())))
    first = RuleSetContent.model_validate({"rules": [rule_body()]})
    second = RuleSetContent.model_validate({"rules": [reordered]})
    assert content_sha256(first) == content_sha256(second)


@pytest.mark.parametrize(
    "overrides",
    [
        {"key": "other-key"},
        {"description": "Why."},
        {"target": "$.schema[0]"},
        {"condition": "false"},
        {"severity": "warning"},
        {"message": "Different."},
        {"match": "any"},
        {"on_empty": "fail"},
        {"fixtures": {"pass": [{"document": {"a": 1}}], "fail": [{"document": {}}]}},
    ],
    ids=[
        "key",
        "description",
        "target",
        "condition",
        "severity",
        "message",
        "match",
        "on_empty",
        "fixtures",
    ],
)
def test_the_hash_changes_when_any_rule_field_changes(overrides: dict[str, Any]) -> None:
    assert content_sha256(content()) != content_sha256(content(**overrides))


def test_the_hash_is_a_64_character_lowercase_hex_digest() -> None:
    digest = content_sha256(content())
    assert len(digest) == 64
    assert digest == digest.lower()
    int(digest, 16)


# --- ctx.operation (quick 261119-rpi) -------------------------------------------------------


def test_a_fixture_ctx_without_operation_serialises_byte_identically_to_before() -> None:
    fixture = Fixture(document={"a": 1}, ctx=FixtureCtx())
    # The literal recorded at HEAD before `operation` existed.
    expected = (
        b'{"ctx":{"folder_kind":"team","folder_path":"fixture","organization_id":null},'
        b'"description":null,"document":{"a":1}}'
    )
    assert canonical_json(fixture.model_dump(mode="json", by_alias=True)) == expected


def test_a_fixture_ctx_operation_is_serialised_when_set() -> None:
    dumped = FixtureCtx(operation="publish").model_dump(mode="json")
    assert dumped["operation"] == "publish"


def test_a_cel1_set_refuses_a_fixture_ctx_operation_and_a_cel2_set_accepts_it() -> None:
    fixture = {"document": {}, "ctx": {"operation": "publish"}}
    body = rule_body(fixtures={"pass": [fixture], "fail": [fixture]})

    with pytest.raises(ValidationError, match="cel-2"):
        RuleSetContent.model_validate({"dsl_version": "cel-1", "rules": [body]})

    accepted = RuleSetContent.model_validate({"dsl_version": "cel-2", "rules": [body]})
    assert accepted.rules[0].fixtures.pass_[0].ctx == FixtureCtx(operation="publish")


# --- cel-2 fixture inputs (D-152) -----------------------------------------------------------


def test_a_fixture_with_previous_and_refs_serialises_both() -> None:
    fixture = Fixture(document={"a": 2}, previous={"a": 1}, refs={"elements": {"x": "found"}})
    dumped = fixture.model_dump(mode="json")
    assert dumped["previous"] == {"a": 1}
    assert dumped["refs"] == {"elements": {"x": "found"}}


def test_a_fixture_without_cel2_inputs_omits_both_keys() -> None:
    dumped = Fixture(document={"a": 2}).model_dump(mode="json")
    assert "previous" not in dumped
    assert "refs" not in dumped
