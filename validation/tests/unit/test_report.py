"""The validation report: blocking semantics, counts, caps and ordering (D-124, D-128, D-139)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from dc_odcs import StructuralError, StructuralReport, validate_structure
from dc_rules.model import RuleDef
from dc_rules.ports import ERROR_MESSAGES
from dc_rules.protocol import FindingWire, UnitResult
from dc_validation.report import (
    STRUCTURAL_MESSAGES,
    RuleBinding,
    UnresolvedAttachment,
    build_report,
    is_blocking,
    pins_of,
    structural_out,
)
from dc_validation.resolution import ResolvedRuleSet

pytestmark = pytest.mark.unit

FOLDER_A = uuid.UUID("00000000-0000-7000-8000-00000000000a")
VALID = StructuralReport(valid=True, error_count=0, errors=())


def rule(key: str = "unique-property-names", severity: str = "error") -> RuleDef:
    return RuleDef.model_validate(
        {
            "key": key,
            "target": "$.schema[*]",
            "condition": "true",
            "severity": severity,
            "message": f"Rule {key} failed.",
            "fixtures": {"pass": [], "fail": []},
        }
    )


def resolved_set(
    name: str = "Org naming",
    *,
    mode: str = "enforce",
    source: str = "mandatory",
    depth: int = 1,
    version_no: int = 1,
    rule_set_id: uuid.UUID | None = None,
    paused: bool = False,
) -> ResolvedRuleSet:
    return ResolvedRuleSet(
        rule_set_id=rule_set_id or uuid.uuid4(),
        rule_set_version_id=uuid.uuid4(),
        name=name,
        scope="folder",
        folder_id=FOLDER_A,
        folder_kind="organization",
        depth=depth,
        version_no=version_no,
        dsl_version="cel-1",
        source=source,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
        enforce_at=datetime(2026, 12, 1, tzinfo=UTC) if mode == "warn" else None,
        paused=paused,
    )


def failing(unit_id: int, *pointers: str) -> UnitResult:
    return UnitResult(
        id=unit_id,
        outcome="fail",
        findings=[FindingWire(outcome="fail", pointer=p, path=f"$[{p}]") for p in pointers],
    )


def passing(unit_id: int) -> UnitResult:
    return UnitResult(id=unit_id, outcome="pass")


def errored(unit_id: int, code: Any = "type_error") -> UnitResult:
    from dc_rules.protocol import error_result

    return error_result(unit_id, code)


def test_a_failed_node_inside_an_errored_unit_stays_a_fail_with_the_rule_message() -> None:
    # `match: each` where one node failed and another errored: the unit outcome is `error`.
    entry = resolved_set()
    mixed = UnitResult(
        id=0,
        outcome="error",
        findings=[
            FindingWire(outcome="fail", pointer="/schema/0", path="$.schema[0]"),
            FindingWire(
                outcome="error", pointer="/schema/1", path="$.schema[1]", error_code="type_error"
            ),
        ],
    )
    report = build_report(VALID, [entry], {0: RuleBinding(entry, rule())}, [mixed])

    failed, errored_finding = sorted(report.findings, key=lambda f: f.location.pointer)
    assert (failed.outcome, failed.message, failed.error_code) == (
        "fail",
        "Rule unique-property-names failed.",
        None,
    )
    assert errored_finding.outcome == "error"
    assert errored_finding.error_code == "type_error"
    assert errored_finding.message == ERROR_MESSAGES["type_error"]
    assert failed.blocking and errored_finding.blocking  # blocking is unchanged


def test_a_failing_enforced_mandatory_error_rule_blocks_and_is_named() -> None:
    entry = resolved_set()
    report = build_report(
        VALID,
        [entry],
        {0: RuleBinding(entry, rule())},
        [failing(0, "/schema/0")],
    )

    assert report.summary.publishable is False
    assert report.summary.blocking_count == 1
    (finding,) = report.findings
    assert finding.rule_key == "unique-property-names"
    assert finding.severity == "error"
    assert finding.outcome == "fail"
    assert finding.blocking is True
    assert finding.location.pointer == "/schema/0"
    assert finding.message == "Rule unique-property-names failed."
    assert finding.rule_set.name == "Org naming"
    assert finding.rule_set.folder_id == FOLDER_A
    assert finding.rule_set.version == 1
    assert finding.error_code is None
    (counts,) = report.rule_sets
    assert (counts.passed, counts.failed, counts.errored) == (0, 1, 0)


def test_a_passing_rule_is_counted_not_listed() -> None:
    entry = resolved_set()
    report = build_report(VALID, [entry], {0: RuleBinding(entry, rule())}, [passing(0)])

    assert report.findings == []
    assert report.summary.publishable is True
    assert report.summary.blocking_count == 0
    assert (report.rule_sets[0].passed, report.rule_sets[0].failed) == (1, 0)


@pytest.mark.parametrize(
    ("severity", "source", "mode", "expected"),
    [
        ("error", "mandatory", "enforce", True),
        ("error", "mandatory", "warn", False),
        ("warning", "mandatory", "enforce", False),
        ("info", "explicit", "enforce", False),
        ("error", "explicit", "enforce", True),
        ("error", "adhoc", "enforce", True),
        ("warning", "adhoc", "enforce", False),
    ],
)
def test_the_blocking_table(severity: str, source: str, mode: str, expected: bool) -> None:
    assert is_blocking(severity, source, mode) is expected


def test_an_error_in_an_enforced_error_rule_blocks_with_the_fixed_text() -> None:
    entry = resolved_set()
    other = rule("other-rule")
    report = build_report(
        VALID,
        [entry],
        {0: RuleBinding(entry, rule()), 1: RuleBinding(entry, other)},
        [errored(0, "type_error"), passing(1)],
    )

    (finding,) = report.findings
    assert finding.outcome == "error"
    assert finding.error_code == "type_error"
    assert finding.message == ERROR_MESSAGES["type_error"]
    assert finding.blocking is True
    assert report.summary.publishable is False
    (counts,) = report.rule_sets
    assert (counts.passed, counts.failed, counts.errored) == (1, 0, 1)


def test_a_unit_with_no_result_is_an_engine_error_never_a_pass() -> None:
    entry = resolved_set()
    report = build_report(VALID, [entry], {0: RuleBinding(entry, rule())}, [])

    (finding,) = report.findings
    assert finding.outcome == "error"
    assert finding.error_code == "engine_error"
    assert finding.blocking is True
    assert report.summary.publishable is False
    assert (report.rule_sets[0].passed, report.rule_sets[0].errored) == (0, 1)


def test_the_same_error_in_a_warn_version_is_reported_and_does_not_block() -> None:
    entry = resolved_set(mode="warn")
    other = rule("other-rule")
    report = build_report(
        VALID,
        [entry],
        {0: RuleBinding(entry, rule()), 1: RuleBinding(entry, other)},
        [errored(0, "type_error"), failing(1, "/schema/0")],
    )

    assert [f.blocking for f in report.findings] == [False, False]
    assert all(f.mode == "warn" and f.enforce_at is not None for f in report.findings)
    assert report.summary.publishable is True
    assert report.summary.blocking_count == 0
    (counts,) = report.rule_sets
    assert (counts.passed, counts.failed, counts.errored) == (0, 1, 1)


def test_a_paused_set_reports_its_findings_without_blocking() -> None:
    entry = resolved_set(mode="warn", paused=True)
    report = build_report(
        VALID,
        [entry],
        {0: RuleBinding(entry, rule())},
        [failing(0, "/schema/0")],
    )

    (finding,) = report.findings
    assert finding.severity == "error"
    assert finding.outcome == "fail"
    assert finding.mode == "warn"
    assert finding.paused is True
    assert finding.blocking is False
    assert finding.enforce_at is None
    assert report.summary.publishable is True
    assert report.summary.blocking_count == 0
    (counts,) = report.rule_sets
    assert (counts.paused, counts.mode, counts.failed) == (True, "warn", 1)
    (pin,) = pins_of([entry])
    assert (pin.paused, pin.mode) == (True, "warn")


def test_an_unpaused_entry_carries_paused_false_everywhere() -> None:
    entry = resolved_set()
    report = build_report(
        VALID, [entry], {0: RuleBinding(entry, rule())}, [failing(0, "/schema/0")]
    )
    assert report.findings[0].paused is False
    assert report.rule_sets[0].paused is False
    assert pins_of([entry])[0].paused is False


def test_an_error_in_a_warning_severity_rule_does_not_block() -> None:
    entry = resolved_set()
    report = build_report(
        VALID, [entry], {0: RuleBinding(entry, rule(severity="warning"))}, [errored(0, "timeout")]
    )

    assert report.findings[0].blocking is False
    assert report.summary.publishable is True


def test_an_unresolved_attachment_blocks_and_never_passes_silently() -> None:
    missing = uuid.uuid4()
    report = build_report(VALID, [], {}, [], [UnresolvedAttachment(missing, "latest", "explicit")])

    (finding,) = report.findings
    assert finding.error_code == "unresolvable_rule_set"
    assert finding.blocking is True
    assert finding.rule_key == "*"
    assert finding.rule_set.id == missing
    assert finding.rule_set.name is None
    assert finding.message == ERROR_MESSAGES["unresolvable_rule_set"]
    assert report.summary.publishable is False


def test_a_structurally_invalid_document_is_not_publishable_without_rule_failures() -> None:
    structural = StructuralReport(
        valid=False, error_count=1, errors=(StructuralError("/id", "m", "type"),)
    )

    report = build_report(structural, [], {}, [])

    assert report.summary.publishable is False
    assert report.summary.blocking_count == 0
    assert report.findings == []


def test_exactly_max_findings_are_all_listed_and_one_more_is_truncated() -> None:
    entry = resolved_set()
    bindings = {0: RuleBinding(entry, rule())}
    exact = build_report(
        VALID, [entry], bindings, [failing(0, *[f"/schema/{i}" for i in range(5)])], max_findings=5
    )
    over = build_report(
        VALID, [entry], bindings, [failing(0, *[f"/schema/{i}" for i in range(6)])], max_findings=5
    )

    assert (len(exact.findings), exact.findings_total, exact.findings_truncated) == (5, 5, False)
    assert (len(over.findings), over.findings_total, over.findings_truncated) == (5, 6, True)
    assert over.summary.blocking_count == 6  # the verdict counts what the cap hides


def test_one_rule_failing_on_two_nodes_gives_two_findings() -> None:
    entry = resolved_set()
    report = build_report(
        VALID, [entry], {0: RuleBinding(entry, rule())}, [failing(0, "/schema/0", "/schema/1")]
    )

    assert [f.location.pointer for f in report.findings] == ["/schema/0", "/schema/1"]
    assert report.rule_sets[0].failed == 1  # counts are per rule


def test_an_empty_document_run_has_no_findings_and_is_publishable() -> None:
    report = build_report(VALID, [], {}, [], [])

    assert report.findings == []
    assert report.rule_sets == []
    assert report.summary.publishable is True
    assert report.summary.blocking_count == 0
    assert report.findings_total == 0
    assert report.findings_truncated is False


def test_findings_are_ordered_by_depth_name_id_rule_key_then_pointer_and_are_deterministic() -> (
    None
):
    deep = resolved_set("Alpha", depth=3)
    shallow_b = resolved_set("beta", depth=1, rule_set_id=uuid.UUID(int=2))
    shallow_a = resolved_set("Alpha", depth=1, rule_set_id=uuid.UUID(int=1))
    bindings = {
        0: RuleBinding(deep, rule("aaa")),
        1: RuleBinding(shallow_b, rule("zzz")),
        2: RuleBinding(shallow_a, rule("mmm")),
        3: RuleBinding(shallow_a, rule("bbb")),
    }
    results = [failing(0, "/x"), failing(1, "/x"), failing(2, "/y", "/b"), failing(3, "/z")]

    first = build_report(VALID, [deep, shallow_b, shallow_a], bindings, results)
    second = build_report(VALID, [shallow_a, deep, shallow_b], bindings, list(reversed(results)))

    order = [(f.rule_set.name, f.rule_key, f.location.pointer) for f in first.findings]
    assert order == [
        ("Alpha", "bbb", "/z"),
        ("Alpha", "mmm", "/b"),
        ("Alpha", "mmm", "/y"),
        ("beta", "zzz", "/x"),
        ("Alpha", "aaa", "/x"),
    ]
    assert first.model_dump_json() == second.model_dump_json()


def test_structural_messages_are_fixed_texts_and_unknown_keywords_get_the_generic_one() -> None:
    report = StructuralReport(
        valid=False,
        error_count=3,
        errors=(
            StructuralError("/a", "RAW LIBRARY TEXT", "type"),
            StructuralError("/b", "RAW LIBRARY TEXT", "someFutureKeyword"),
            StructuralError("/c", "RAW LIBRARY TEXT"),
        ),
    )

    out = structural_out(report)

    assert [e.message for e in out.errors] == [
        STRUCTURAL_MESSAGES["type"],
        "The value does not conform to the ODCS schema.",
        "The value does not conform to the ODCS schema.",
    ]
    assert "RAW LIBRARY TEXT" not in out.model_dump_json()


def test_pins_record_warn_mode_dsl_version_and_the_deprecation_hint() -> None:
    enforced = resolved_set("Org naming", version_no=1)
    warn = resolved_set("Org naming", version_no=2, mode="warn", rule_set_id=enforced.rule_set_id)

    pins = pins_of([warn, enforced], deprecated_versions={enforced.rule_set_version_id})

    assert [(p.version, p.mode, p.dsl_version, p.deprecated_version) for p in pins] == [
        (1, "enforce", "cel-1", True),
        (2, "warn", "cel-1", False),
    ]


SECRET_VALUE = "SECRET-VALUE-123"
SECRET_KEY = "SECRET-KEY-9"


def planted_document() -> dict[str, Any]:
    return {
        "apiVersion": "v3.2.0",
        "kind": "DataContract",
        "id": "c-1",
        "version": "1.0.0",
        "status": "active",
        SECRET_KEY: "x",
        "schema": [
            {
                "name": "t",
                "properties": [
                    {
                        "name": "a",
                        "logicalType": SECRET_VALUE,
                        "logicalTypeOptions": {"maxLength": SECRET_VALUE, "pattern": 7},
                    },
                    {"name": "b", "logicalType": "string", "description": 5},
                ],
            }
        ],
    }


def test_no_document_value_reaches_the_serialised_report() -> None:
    document = planted_document()
    structural = validate_structure(document, "v3.2.0")
    assert structural.valid is False
    # the library message does embed the value: this is the leak the report must not carry
    assert any(SECRET_VALUE in e.message for e in structural.errors)

    entry = resolved_set()
    report = build_report(
        structural,
        [entry],
        {0: RuleBinding(entry, rule()), 1: RuleBinding(entry, rule("second-rule"))},
        [errored(0, "invalid_argument"), failing(1, "/schema/0/properties/0")],
    )

    text = json.dumps(report.model_dump(mode="json"))
    assert SECRET_VALUE not in text
    # Quick 261119-wk8 (deliberate): an unknown-property finding now names the offending KEY so the
    # author can find it. Keys only: the planted value next to it must still never appear.
    assert any(SECRET_KEY in e.message for e in report.structural.errors)
    assert report.structural.error_count >= 3
    assert {e.keyword for e in report.structural.errors} >= {"type", "enum"}


def test_a_report_with_a_native_blocking_finding_and_a_cut_list_round_trips() -> None:
    from dc_odcs.diff import compare
    from dc_validation.compat import BaselineRef, build_compatibility
    from dc_validation.report import ValidationReportOut

    before = {"schema": [{"name": "t", "properties": [{"name": "a"}, {"name": "b"}]}]}
    after = {"version": "1.1.0", "schema": [{"name": "t", "properties": [{"name": "a"}]}]}
    compat = build_compatibility(
        mode="publish",
        baseline=BaselineRef(version_id=None, semver="1.0.0"),
        result=compare(before, after),
        declared="1.1.0",
        stable_major_floor=1,
    )
    noisy = [
        UnitResult(
            id=n, outcome="fail", findings=[FindingWire(outcome="fail", pointer="", path="$")]
        )
        for n in range(3)
    ]
    bindings = {n: RuleBinding(resolved=resolved_set(), rule=rule(f"rule-{n}")) for n in range(3)}

    report = build_report(
        VALID,
        [],
        bindings,
        noisy,
        max_findings=1,
        native_findings=compat.findings,
        compatibility=compat.section,
    )

    assert report.findings_truncated is True
    assert report.findings[0].source == "native"  # listed first, so the cap never hides it
    assert report.summary.blocking_count == 1 + 3
    assert report.summary.publishable is False
    wire = json.loads(report.model_dump_json())
    assert wire["compatibility"]["changes"][0]["class"] == "breaking"
    assert ValidationReportOut.model_validate(wire) == report
    assert ValidationReportOut.model_validate({**wire, "compatibility": None}).compatibility is None


def test_a_report_stored_before_compatibility_existed_still_loads() -> None:
    from dc_validation.report import ValidationReportOut

    old = json.loads(build_report(VALID, [], {}, []).model_dump_json())
    del old["compatibility"]

    assert ValidationReportOut.model_validate(old).compatibility is None


def test_unknown_property_errors_name_the_keys_and_nothing_else() -> None:
    """Quick 261119-wk8: the sentence names the offending KEYS (bounded), never values."""
    report = StructuralReport(
        valid=False,
        error_count=2,
        errors=(
            StructuralError(
                "", "RAW", "additionalProperties", unexpected=("owner", "tags"), unexpected_count=2
            ),
            StructuralError(
                "/schema/0",
                "RAW TOPSECRET",
                "unevaluatedProperties",
                unexpected=("a", "b", "c", "d", "e"),
                unexpected_count=9,
            ),
        ),
    )

    out = structural_out(report)

    assert [e.message for e in out.errors] == [
        "The object has properties the schema does not allow: `owner`, `tags`.",
        "The object has properties the schema does not allow: `a`, `b`, `c`, `d`, `e` and 4 more.",
    ]
    assert [e.pointer for e in out.errors] == ["", "/schema/0"]
    assert "TOPSECRET" not in out.model_dump_json()
