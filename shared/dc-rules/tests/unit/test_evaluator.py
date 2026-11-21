"""Evaluator semantics and the compile-time / evaluation-time error-mapping tables (plan 05-01).

Everything here runs the real `CelEngine` and `JsonPathTargeter`: the point is to pin what the
engines actually return, not a model of them.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest
from dc_rules.cel_engine import CelEngine, engine_version, map_engine_text
from dc_rules.evaluator import evaluate_rule, references_contract
from dc_rules.model import RuleDef
from dc_rules.ports import Limits, RuleCompileError, RuleResult
from dc_rules.targets import JsonPathTargeter
from pydantic import ValidationError

pytestmark = pytest.mark.unit

ENGINE = CelEngine()
TARGETER = JsonPathTargeter()

UNIQUE_NAMES = "this.properties.map(p, p.name).distinct().size() == this.properties.size()"


def make_rule(condition: str, target: str = "$", **overrides: Any) -> RuleDef:
    body: dict[str, Any] = {
        "key": "rule-under-test",
        "target": target,
        "condition": condition,
        "severity": "error",
        "message": "Rule under test.",
        "fixtures": {"pass": [{"document": {}}], "fail": [{"document": {}}]},
    }
    body.update(overrides)
    return RuleDef.model_validate(body)


def run(
    rule: RuleDef, document: Any, ctx: dict[str, Any] | None = None, limits: Limits | None = None
) -> RuleResult:
    return evaluate_rule(
        rule,
        ENGINE.compile(rule.condition),
        TARGETER.compile(rule.target),
        document,
        ctx or {},
        engine=ENGINE,
        targeter=TARGETER,
        limits=limits or Limits(),
    )


def table_doc(*property_names: str) -> dict[str, Any]:
    return {
        "schema": [
            {"name": "orders", "properties": [{"name": name} for name in property_names]},
        ]
    }


# --- pass / fail with locations -------------------------------------------------------------


def test_duplicate_property_names_fail_at_the_table_with_pointer_and_path() -> None:
    result = run(make_rule(UNIQUE_NAMES, "$.schema[*]"), table_doc("id", "id"))
    assert result.outcome == "fail"
    assert result.matched == 1
    (finding,) = result.findings
    assert finding.outcome == "fail"
    assert finding.error_code is None
    assert finding.location.pointer == "/schema/0"
    assert finding.location.path == "$['schema'][0]"


def test_unique_property_names_pass_without_findings() -> None:
    result = run(make_rule(UNIQUE_NAMES, "$.schema[*]"), table_doc("id", "name"))
    assert result.outcome == "pass"
    assert result.findings == ()


def test_the_default_target_is_the_document_root() -> None:
    rule = make_rule("has(this.schema)")
    assert rule.target == "$"
    assert run(rule, {"schema": []}).outcome == "pass"
    failing = run(rule, {})
    assert failing.outcome == "fail"
    assert failing.findings[0].location.pointer == ""


def test_ctx_and_contract_bindings_are_available() -> None:
    rule = make_rule("ctx.folder_kind == 'team' && contract.kind == 'DataContract'")
    assert run(rule, {"kind": "DataContract"}, {"folder_kind": "team"}).outcome == "pass"
    assert run(rule, {"kind": "DataContract"}, {"folder_kind": "project"}).outcome == "fail"


# --- evaluation-time errors never read as pass or fail ------------------------------------------


@pytest.mark.parametrize(
    ("condition", "document", "code"),
    [
        ("this.missing == 1", {}, "no_such_key"),
        ("this.name + 1 == 2", {"name": "a"}, "type_error"),
        ("this.name", {"name": "a"}, "not_boolean"),
        ("this.n / 0 == 1", {"n": 1}, "invalid_argument"),
        ("this.l.all(i, this.l.all(j, true))", {"l": list(range(200))}, "budget_exceeded"),
    ],
)
def test_an_evaluation_error_is_error_with_its_code(
    condition: str, document: Any, code: str
) -> None:
    result = run(make_rule(condition), document)
    assert result.outcome == "error"
    (finding,) = result.findings
    assert finding.outcome == "error"
    assert finding.error_code == code


def test_a_raised_runtime_error_and_a_returned_error_value_both_become_error() -> None:
    returned = run(make_rule("this.missing == 1"), {})  # arrives as an ERROR value
    raised = run(make_rule("this.l.all(i, this.l.all(j, true))"), {"l": list(range(200))})
    assert returned.outcome == raised.outcome == "error"
    assert returned.findings[0].error_code == "no_such_key"
    assert raised.findings[0].error_code == "budget_exceeded"


def test_there_is_no_rule_field_that_decides_what_an_error_means() -> None:
    """RULE-09: an author cannot map an evaluation error to pass or fail."""
    for field in RuleDef.model_fields:
        assert "error" not in field
        assert "exception" not in field
    with pytest.raises(ValidationError):
        make_rule("true", on_error="pass")
    with pytest.raises(ValidationError):
        make_rule("true", on_error_outcome="pass")


def test_an_error_finding_never_carries_the_engine_text() -> None:
    result = run(make_rule("this.missing == 1"), {"secret": "hunter2"})
    text = repr(result)
    assert "Key not found" not in text
    assert "NOT_FOUND" not in text
    assert "hunter2" not in text
    big = run(make_rule("this.big == 1"), {"big": 2**70})  # engine text echoes the integer
    assert "1180591620717411303424" not in repr(big)


# --- on_empty / match modes ----------------------------------------------------------------


def test_zero_matches_pass_by_default() -> None:
    result = run(make_rule("true", "$.nothing[*]"), {})
    assert result.outcome == "pass"
    assert result.matched == 0


def test_zero_matches_fail_when_on_empty_is_fail() -> None:
    rule = make_rule("true", "$.nothing[*]", on_empty="fail")
    result = run(rule, {})
    assert result.outcome == "fail"
    (finding,) = result.findings
    assert finding.location.pointer == ""
    assert finding.location.path == rule.target


def test_match_any_passes_when_one_node_is_true() -> None:
    rule = make_rule("this.ok", "$.items[*]", match="any")
    result = run(rule, {"items": [{"ok": False}, {"ok": True}, {"ok": False}]})
    assert result.outcome == "pass"
    assert result.findings == ()


def test_match_any_fails_once_at_the_target_when_no_node_is_true() -> None:
    rule = make_rule("this.ok", "$.items[*]", match="any")
    result = run(rule, {"items": [{"ok": False}, {"ok": False}, {"ok": False}]})
    assert result.outcome == "fail"
    (finding,) = result.findings
    assert finding.location.pointer == ""
    assert finding.location.path == rule.target


def test_match_any_reports_an_error_when_a_node_errored_and_none_was_true() -> None:
    rule = make_rule("this.ok", "$.items[*]", match="any")
    result = run(rule, {"items": [{"ok": False}, {}, {"ok": False}]})
    assert result.outcome == "error"
    (finding,) = result.findings
    assert finding.location.pointer == ""
    assert finding.error_code == "no_such_key"


def test_match_each_errors_never_stop_the_remaining_nodes() -> None:
    rule = make_rule("this.ok", "$.items[*]")
    result = run(rule, {"items": [{"ok": True}, {}, {"ok": False}]})
    assert result.outcome == "error"
    assert result.matched == 3
    by_pointer = {f.location.pointer: f for f in result.findings}
    assert set(by_pointer) == {"/items/1", "/items/2"}
    assert by_pointer["/items/1"].outcome == "error"
    assert by_pointer["/items/1"].error_code == "no_such_key"
    assert by_pointer["/items/2"].outcome == "fail"
    assert by_pointer["/items/2"].error_code is None


# --- match caps -----------------------------------------------------------------------------


def test_references_contract_ignores_strings_and_field_access() -> None:
    assert references_contract("contract.schema.size() > 0") is True
    assert references_contract("has(contract.id)") is True
    assert references_contract("this.name == 'contract'") is False
    assert references_contract('this.name == "contract"') is False
    assert references_contract("this.contract == 1") is False
    assert references_contract("this.contracts.size() > 0") is False


def test_a_contract_referencing_rule_is_capped_at_100_matches() -> None:
    rule = make_rule("contract.items.size() > 0", "$.items[*]")
    at_cap = run(rule, {"items": [{}] * 100})
    assert at_cap.outcome == "pass"
    over = run(rule, {"items": [{}] * 101})
    assert over.outcome == "error"
    (finding,) = over.findings
    assert finding.error_code == "too_many_matches"
    assert finding.location.pointer == ""
    assert finding.location.path == rule.target


def test_a_rule_without_contract_is_capped_at_10000_matches() -> None:
    rule = make_rule("true", "$.items[*]")
    assert run(rule, {"items": [0] * 10_000}).outcome == "pass"
    over = run(rule, {"items": [0] * 10_001})
    assert over.outcome == "error"
    assert over.findings[0].error_code == "too_many_matches"


def test_a_target_that_recurses_too_deeply_is_a_target_error() -> None:
    deep: Any = []
    for _ in range(3000):
        deep = [deep]
    result = run(make_rule("true", "$..*"), deep)
    assert result.outcome == "error"
    assert result.findings[0].error_code == "target_error"


# --- compile-time table -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "previous.x == 1",  # undeclared identifier (D-111)
        "now() > 0",  # unknown function, no time of day
        "this.a ==",  # syntax error
        "(" * 33 + "true" + ")" * 33,  # parser recursion limit of 32
        "true && " * 14_300 + "true",  # more than the 100,000-codepoint limit
    ],
)
def test_every_compile_failure_is_a_compile_error(expression: str) -> None:
    with pytest.raises(RuleCompileError) as caught:
        ENGINE.compile(expression)
    assert caught.value.code == "compile_error"
    assert caught.value.code != "engine_error"


def test_a_non_boolean_return_type_is_not_boolean_at_compile_time() -> None:
    with pytest.raises(RuleCompileError) as caught:
        ENGINE.compile("1 + 1")
    assert caught.value.code == "not_boolean"


def test_a_dyn_condition_compiles() -> None:
    assert ENGINE.compile("this.a") is not None


def test_an_invalid_target_is_target_invalid() -> None:
    with pytest.raises(RuleCompileError) as caught:
        TARGETER.compile("$.a[?(")
    assert caught.value.code == "target_invalid"


@pytest.mark.parametrize("function", ["match", "search"])
def test_the_regex_filter_functions_are_not_available_in_targets(function: str) -> None:
    with pytest.raises(RuleCompileError) as caught:
        TARGETER.compile(f"$.a[?{function}(@.n, '(a+)+$')]")
    assert caught.value.code == "target_invalid"


# --- evaluation-time text table (engine strings captured by the research spike) -----------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ('NOT_FOUND: Key not found in map : "y"', "no_such_key"),
        ("UNKNOWN: No matching overloads found : _+_(string, int64)", "type_error"),
        ("INVALID_ARGUMENT: divide by zero", "invalid_argument"),
        (
            "INVALID_ARGUMENT: Integer value '1180591620717411303424' out of range for "
            "'this['big'] -> 1180591620717411303424'.",
            "invalid_argument",
        ),
        (
            "INVALID_ARGUMENT: invalid regular expression: bad repetition operator: {1000}{1000}",
            "invalid_argument",
        ),
        ("INTERNAL: Iteration budget exceeded [INTERNAL]", "budget_exceeded"),
        ("UNKNOWN: something nobody has seen before", "engine_error"),
        ("", "engine_error"),
    ],
)
def test_map_engine_text_maps_each_captured_engine_string(text: str, code: str) -> None:
    assert map_engine_text(text) == code


# --- packaging ---------------------------------------------------------------------------------


def test_the_package_root_does_not_load_the_cel_engine() -> None:
    probe = (
        "import sys, dc_rules; "
        "assert 'cel_expr_python' not in sys.modules; "
        "assert 'dc_rules.cel_engine' not in sys.modules; "
        "assert 'dc_rules.evaluator' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)  # noqa: S603


def test_the_engine_version_names_both_engines_with_their_pins() -> None:
    version = engine_version()
    assert "cel-expr-python 0.1.4" in version
    assert "python-jsonpath 2.2.2" in version


# --- normalisation (iterative, never mutates) ---------------------------------------------------


def test_normalize_turns_out_of_range_integers_into_floats() -> None:
    from dc_rules.normalize import normalize_document

    document = {"a": 2**70, "b": [-(2**70), {"c": 2**64}], "d": 2**63 - 1, "e": -(2**63)}
    out = normalize_document(document)
    assert isinstance(out["a"], float)
    assert isinstance(out["b"][0], float)
    assert isinstance(out["b"][1]["c"], float)  # 2**64 is one above the uint64 maximum
    assert out["d"] == 2**63 - 1
    assert isinstance(out["d"], int)
    assert out["e"] == -(2**63)
    assert isinstance(out["e"], int)


def test_normalize_keeps_in_range_unsigned_64_bit_integers() -> None:
    from dc_rules.normalize import normalize_document

    out = normalize_document({"u": 2**64 - 1})
    assert out["u"] == 2**64 - 1
    assert isinstance(out["u"], int)


def test_normalize_never_mutates_its_input() -> None:
    from dc_rules.normalize import normalize_document

    document = {"a": [2**70, {"b": 2**70}]}
    out = normalize_document(document)
    assert isinstance(document["a"][0], int)
    assert isinstance(document["a"][1]["b"], int)  # type: ignore[index]
    assert out is not document
    assert out["a"] is not document["a"]


def test_normalize_handles_a_10000_deep_list_without_recursion() -> None:
    from dc_rules.normalize import normalize_document

    deep: Any = [2**70]
    for _ in range(9_999):
        deep = [deep]
    out = normalize_document(deep)
    depth = 0
    while isinstance(out, list):
        out = out[0]
        depth += 1
    assert depth == 10_000
    assert isinstance(out, float)


def test_a_big_integer_no_longer_poisons_every_rule_over_the_subtree() -> None:
    from dc_rules.normalize import normalize_document

    document = {"items": [{"n": 1}, {"n": 2**70}]}
    rule = make_rule("this.n > 0", "$.items[*]")
    assert run(rule, document).outcome == "error"  # raw: the engine rejects the big integer
    assert run(rule, normalize_document(document)).outcome == "pass"
