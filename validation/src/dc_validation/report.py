"""The validation report: one shape for every mode, value-free by construction (D-124, D-128).

A report answers three questions without ever echoing a document value (VAL-07): which rules failed
and where (``findings``, with JSON Pointer locations), whether the contract may be published
(``summary.publishable``) and what the ODCS JSON Schema said (``structural``). Reports are stored
forever and may be read by principals without contract read, so every text in them is either the
rule author's static message, a fixed error text (``ERROR_MESSAGES``) or a fixed structural text
(``STRUCTURAL_MESSAGES``). The library message of a structural error is never forwarded: it embeds
the failing instance. The one addition is the unknown-property sentence, which names the offending
property KEYS (never values; at most five, 64 characters each, control characters removed, built
by ``dc_odcs.unknown_properties_message``; quick 261119-wk8).

Blocking (D-124): a finding blocks when its severity is ``error`` and it comes from the enforced
version of a mandatory set, an explicit attachment or an ad-hoc set. An evaluation error follows the
same rule (D-139), so an error in a rule that would block, blocks (fail closed). ``publishable`` is
``structural.valid and blocking_count == 0`` (D-129): schema violations block in every mode.

A paused platform set (quick 261118-vpa) resolves in mode ``warn`` with ``paused: true``, so D-124
already makes its findings non-blocking; they are still listed, marked paused, with no
``enforce_at`` (a paused set has no effective enforcement date).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from dc_odcs import StructuralError, StructuralReport, unknown_properties_message
from dc_rules.model import RuleDef
from dc_rules.ports import ERROR_MESSAGES, ErrorCode
from dc_rules.protocol import FindingWire, UnitResult
from pydantic import BaseModel, ConfigDict, Field

from dc_validation.resolution import Mode, ResolvedRuleSet, Source, UnresolvedAttachment

DEFAULT_MAX_FINDINGS = 200

# Findings the platform itself raises (the semver rule, an unusable baseline). They belong to no
# rule set, are always on and cannot be attached, disabled or downgraded (D-147). Only findings
# carry this source: pins and per-set counts keep ``Source`` (research Pitfall 6).
NATIVE_SOURCE: Final = "native"
FindingSource = Source | Literal["native"]

GENERIC_STRUCTURAL_MESSAGE = "The value does not conform to the ODCS schema."

# Fixed text per JSON Schema keyword. The keyword comes from the vendored schema (never from the
# document), the text is a constant, so a structural error carries no document value.
_RANGE = "The value is outside the allowed range."
_ALLOWED = "The value is not one of the allowed values."
_FORMAT = "The value does not have the required format."
_UNKNOWN_PROPERTIES = "The object has properties the schema does not allow."
_NO_MATCH = "The value does not match any allowed form."
STRUCTURAL_MESSAGES: Mapping[str, str] = {
    "required": "A required property is missing.",
    "type": "The value has the wrong type.",
    "unevaluatedProperties": _UNKNOWN_PROPERTIES,
    "additionalProperties": _UNKNOWN_PROPERTIES,
    "enum": _ALLOWED,
    "const": _ALLOWED,
    "pattern": _FORMAT,
    "format": _FORMAT,
    "minLength": _RANGE,
    "maxLength": _RANGE,
    "minimum": _RANGE,
    "maximum": _RANGE,
    "exclusiveMinimum": _RANGE,
    "exclusiveMaximum": _RANGE,
    "minItems": _RANGE,
    "maxItems": _RANGE,
    "minProperties": _RANGE,
    "maxProperties": _RANGE,
    "uniqueItems": "The list contains duplicate entries.",
    "oneOf": _NO_MATCH,
    "anyOf": _NO_MATCH,
}

# The rule-set id of an attachment that could not be resolved is reported as an error finding.
UNRESOLVABLE: ErrorCode = "unresolvable_rule_set"


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidationRuleSetRefOut(_Out):
    # null on a native finding, which has no rule set
    id: uuid.UUID | None
    name: str | None
    folder_id: uuid.UUID | None
    folder_kind: str | None
    scope: str | None
    version: int | None


class ValidationLocationOut(_Out):
    pointer: str
    path: str


class ValidationFindingOut(_Out):
    rule_set: ValidationRuleSetRefOut
    source: FindingSource
    mode: Mode
    enforce_at: datetime | None
    # True when the finding's set is paused (a platform set a Global admin paused): warn only.
    paused: bool = False
    rule_key: str
    severity: Literal["error", "warning", "info"]
    outcome: Literal["fail", "error"]
    message: str
    location: ValidationLocationOut
    blocking: bool
    error_code: ErrorCode | None


class ValidationRuleSetCountsOut(_Out):
    rule_set_id: uuid.UUID
    version: int
    source: Source
    mode: Mode
    paused: bool = False
    passed: int
    failed: int
    errored: int


class ValidationStructuralErrorOut(_Out):
    pointer: str
    keyword: str
    message: str


class ValidationStructuralOut(_Out):
    valid: bool
    error_count: int
    errors: list[ValidationStructuralErrorOut]


class ValidationSummaryOut(_Out):
    publishable: bool
    blocking_count: int


ChangeValue = str | int | float | bool | None | list[str | int | float | bool | None]


class ValidationBaselineRefOut(_Out):
    """The version a run compared against (no document content)."""

    version_id: uuid.UUID | None
    semver: str | None


class ValidationChangeCountsOut(_Out):
    breaking: int
    warning: int
    info: int


class ValidationChangeOut(_Out):
    """One classified change. ``before`` and ``after`` are set only for allow-listed attributes."""

    model_config = ConfigDict(
        extra="forbid", serialize_by_alias=True, validate_by_alias=True, validate_by_name=True
    )

    code: str
    change_class: Literal["breaking", "warning", "info"] = Field(alias="class")
    path: str
    pointer_before: str | None
    pointer_after: str | None
    attribute: str | None
    before: ChangeValue
    after: ChangeValue


class ValidationCompatibilityOut(_Out):
    """The compatibility section: changes against the baseline and the semver verdict."""

    status: Literal["compared", "no-baseline", "unavailable"]
    baseline: ValidationBaselineRefOut | None
    classifier_version: str | None
    declared_version: str | None
    required_bump: Literal["major", "minor", "patch", "none"] | None
    bump_ok: bool | None
    counts: ValidationChangeCountsOut
    changes: list[ValidationChangeOut]
    changes_total: int
    changes_truncated: bool


class ValidationRefCountsOut(_Out):
    """How many references of one kind were found, not found or could not be checked."""

    found: int
    not_found: int
    unverifiable: int


class ValidationLinkCountsOut(_Out):
    """How many contract links are published, not published yet, missing or unchecked."""

    published: int
    unpublished_or_draft_only: int
    not_found: int
    unverifiable: int


class ValidationRefFactsSummaryOut(_Out):
    """Counts per kind and state of the reference facts a run resolved.

    Counts only: no reference string, user name or team name is ever stored in a report.
    """

    elements: ValidationRefCountsOut
    servers: ValidationRefCountsOut
    principals: ValidationRefCountsOut
    teams: ValidationRefCountsOut
    contracts: ValidationLinkCountsOut


class ValidationReportOut(_Out):
    status: Literal["completed"] = "completed"
    structural: ValidationStructuralOut
    findings: list[ValidationFindingOut]
    findings_total: int
    findings_truncated: bool
    rule_sets: list[ValidationRuleSetCountsOut]
    summary: ValidationSummaryOut
    # None only for a report stored before compatibility existed (D-157); every new run sets it.
    compatibility: ValidationCompatibilityOut | None = None
    # None only for a report stored before reference facts existed (D-157).
    ref_facts: ValidationRefFactsSummaryOut | None = None


class ValidationPinOut(_Out):
    rule_set_id: uuid.UUID
    name: str
    folder_id: uuid.UUID | None
    scope: str
    version: int
    source: Source
    mode: Mode
    dsl_version: str
    deprecated_version: bool
    paused: bool = False


@dataclass(frozen=True)
class RuleBinding:
    """The rule a worker unit id stands for, and the resolved rule-set version it came from."""

    resolved: ResolvedRuleSet
    rule: RuleDef


def _structural_message(error: StructuralError) -> str:
    """The fixed text per keyword; an unknown-property error also names the offending keys.

    The keys come from ``dc_odcs`` already cleaned and bounded (keys only, never values; quick
    261119-wk8). No other document-derived text is ever added.
    """
    if error.keyword in ("unevaluatedProperties", "additionalProperties"):
        return unknown_properties_message(error.unexpected, error.unexpected_count)
    return STRUCTURAL_MESSAGES.get(error.keyword, GENERIC_STRUCTURAL_MESSAGE)


def structural_out(report: StructuralReport) -> ValidationStructuralOut:
    """Sanitise a structural report: pointer, schema keyword and a fixed text, never ``message``."""
    errors = [
        ValidationStructuralErrorOut(
            pointer=error.pointer,
            keyword=error.keyword,
            message=_structural_message(error),
        )
        for error in report.errors
    ]
    return ValidationStructuralOut(
        valid=report.valid, error_count=report.error_count, errors=errors
    )


def is_blocking(severity: str, source: str, mode: str) -> bool:
    """D-124: only an error-severity finding from an enforced mandatory, explicit or ad-hoc set."""
    if severity != "error":
        return False
    return source in ("explicit", "adhoc") or (source == "mandatory" and mode == "enforce")


def _ref(resolved: ResolvedRuleSet) -> ValidationRuleSetRefOut:
    return ValidationRuleSetRefOut(
        id=resolved.rule_set_id,
        name=resolved.name,
        folder_id=resolved.folder_id,
        folder_kind=resolved.folder_kind,
        scope=resolved.scope,
        version=resolved.version_no,
    )


def _sort_key(finding: ValidationFindingOut, depth: int) -> tuple[object, ...]:
    return (
        depth,
        (finding.rule_set.name or "").lower(),
        str(finding.rule_set.id),
        finding.rule_key,
        finding.location.pointer,
        finding.rule_set.version or 0,
        finding.location.path,
        finding.outcome,
    )


def _unit_findings(
    binding: RuleBinding, result: UnitResult
) -> tuple[list[ValidationFindingOut], Literal["pass", "fail", "error"]]:
    resolved, rule = binding.resolved, binding.rule
    if result.outcome == "pass":
        return [], "pass"
    outcome: Literal["fail", "error"]
    # "invalid" is an authoring-check outcome that evaluation never yields: fail closed as an error.
    outcome = "fail" if result.outcome == "fail" else "error"
    blocking = is_blocking(rule.severity, resolved.source, resolved.mode)
    findings = list(result.findings)
    if not findings:
        # a verdict without a location is still reported, at the document root
        findings = [_root_finding(outcome, result.error_code)]
    out: list[ValidationFindingOut] = []
    for finding in findings:
        # The finding speaks for itself: a node that failed stays a `fail` even when another node
        # of the same unit errored (the unit outcome only feeds the counts).
        finding_outcome: Literal["fail", "error"] = "fail" if finding.outcome == "fail" else "error"
        code: ErrorCode | None = None
        if finding_outcome == "error":
            code = finding.error_code or result.error_code or "engine_error"
            message = ERROR_MESSAGES[code]
        else:
            message = rule.message
        out.append(
            ValidationFindingOut(
                rule_set=_ref(resolved),
                source=resolved.source,
                mode=resolved.mode,
                enforce_at=(
                    resolved.enforce_at if resolved.mode == "warn" and not resolved.paused else None
                ),
                paused=resolved.paused,
                rule_key=rule.key,
                severity=rule.severity,
                outcome=finding_outcome,
                message=message,
                location=ValidationLocationOut(pointer=finding.pointer, path=finding.path),
                blocking=blocking,
                error_code=code,
            )
        )
    return out, outcome


def _root_finding(outcome: Literal["fail", "error"], code: ErrorCode | None) -> FindingWire:
    return FindingWire(outcome=outcome, pointer="", path="$", error_code=code)


def build_report(
    structural: StructuralReport,
    resolved: Sequence[ResolvedRuleSet],
    rules_by_unit: Mapping[int, RuleBinding],
    results: Sequence[UnitResult],
    unresolved: Sequence[UnresolvedAttachment] = (),
    max_findings: int = DEFAULT_MAX_FINDINGS,
    native_findings: Sequence[ValidationFindingOut] = (),
    compatibility: ValidationCompatibilityOut | None = None,
    ref_facts: ValidationRefFactsSummaryOut | None = None,
) -> ValidationReportOut:
    """Build the one report shape. Deterministic: the same input gives byte-identical output.

    ``native_findings`` are the platform's own (already blocking) findings; like an unresolvable
    attachment they are listed first, so the findings cap never hides them, and they count toward
    ``blocking_count``.
    """
    by_unit = {result.id: result for result in results}
    keyed: list[tuple[tuple[object, ...], ValidationFindingOut]] = []
    counts: dict[uuid.UUID, list[int]] = {r.rule_set_version_id: [0, 0, 0] for r in resolved}

    for unit_id in sorted(rules_by_unit):
        binding = rules_by_unit[unit_id]
        result = by_unit.get(unit_id)
        if result is None:
            # no verdict for a unit that should have run: an error, never a pass
            result = UnitResult(id=unit_id, outcome="error", error_code="engine_error")
        findings, outcome = _unit_findings(binding, result)
        tally = counts.setdefault(binding.resolved.rule_set_version_id, [0, 0, 0])
        tally[("pass", "fail", "error").index(outcome)] += 1
        keyed.extend((_sort_key(f, binding.resolved.depth), f) for f in findings)

    for attachment in unresolved:
        spec = attachment.version_spec
        finding = ValidationFindingOut(
            rule_set=ValidationRuleSetRefOut(
                id=attachment.rule_set_id,
                name=None,
                folder_id=None,
                folder_kind=None,
                scope=None,
                version=int(spec) if spec.isdigit() else None,
            ),
            source=attachment.source,
            mode="enforce",
            enforce_at=None,
            rule_key="*",
            severity="error",
            outcome="error",
            message=ERROR_MESSAGES[UNRESOLVABLE],
            location=ValidationLocationOut(pointer="", path="$"),
            blocking=True,
            error_code=UNRESOLVABLE,
        )
        # depth -1: an attachment that cannot resolve is listed first, so a cap never hides it
        keyed.append((_sort_key(finding, -1), finding))

    for native in native_findings:
        keyed.append((_sort_key(native, -1), native))

    keyed.sort(key=lambda pair: pair[0])
    all_findings = [finding for _, finding in keyed]
    blocking_count = sum(1 for finding in all_findings if finding.blocking)
    structural_report = structural_out(structural)

    rule_sets = [
        ValidationRuleSetCountsOut(
            rule_set_id=entry.rule_set_id,
            version=entry.version_no,
            source=entry.source,
            mode=entry.mode,
            paused=entry.paused,
            passed=counts[entry.rule_set_version_id][0],
            failed=counts[entry.rule_set_version_id][1],
            errored=counts[entry.rule_set_version_id][2],
        )
        for entry in sorted(resolved, key=_resolved_order)
    ]
    return ValidationReportOut(
        structural=structural_report,
        findings=all_findings[:max_findings],
        findings_total=len(all_findings),
        findings_truncated=len(all_findings) > max_findings,
        rule_sets=rule_sets,
        summary=ValidationSummaryOut(
            publishable=structural_report.valid and blocking_count == 0,
            blocking_count=blocking_count,
        ),
        compatibility=compatibility,
        ref_facts=ref_facts,
    )


def _resolved_order(entry: ResolvedRuleSet) -> tuple[int, str, str, int, int]:
    return (
        entry.depth,
        entry.name.lower(),
        str(entry.rule_set_id),
        0 if entry.mode == "enforce" else 1,
        entry.version_no,
    )


def pins_of(
    resolved: Sequence[ResolvedRuleSet],
    deprecated_versions: Collection[uuid.UUID] = (),
) -> list[ValidationPinOut]:
    """The rule-set versions a run applied, for the stored run and the published version (D-132).

    A warn-mode version is pinned with ``mode: warn``. ``deprecated_versions`` holds the
    ``rule_set_version_id`` values known to be deprecated (a hint, never a gate).
    """
    return [
        ValidationPinOut(
            rule_set_id=entry.rule_set_id,
            name=entry.name,
            folder_id=entry.folder_id,
            scope=entry.scope,
            version=entry.version_no,
            source=entry.source,
            mode=entry.mode,
            dsl_version=entry.dsl_version,
            deprecated_version=entry.rule_set_version_id in deprecated_versions,
            paused=entry.paused,
        )
        for entry in sorted(resolved, key=_resolved_order)
    ]
