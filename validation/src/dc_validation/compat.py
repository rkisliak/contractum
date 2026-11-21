"""The compatibility section and the platform's own findings of a validation run (D-147, D-148).

Validation is the pure function that decides (D-147): it diffs the document against a baseline the
caller sends and turns the verdict into one report section plus, when needed, *native* findings.
A native finding belongs to no rule set: it is always on, there is no request field, setting, header
or role that skips or downgrades it, and it counts in ``blocking_count`` like any blocking finding
(D-143, VAL-03). Two exist:

* ``semver-breaking-change``: the document breaks its consumers (a breaking change against the
  baseline) without declaring a major version above the highest stable major already published.
* ``compatibility-unavailable``: in publish mode the baseline could not be compared (over the size
  cap, unparseable, or not a supported ODCS document), so the gate fails closed. In save mode the
  same condition is advisory: the section says ``unavailable`` and nothing blocks.

Every text here is fixed. The only document-derived values are the allow-listed attribute values
and the name-keyed paths of the changes (T-6-15) and the declared version, which is shown only when
it looks like a version string.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Final, Literal

from dc_odcs import MAX_VERSION_CHARS
from dc_odcs.diff import DiffResult, required_bump
from dc_odcs.diff.model import value_allowed

from dc_validation.report import (
    NATIVE_SOURCE,
    ChangeValue,
    ValidationBaselineRefOut,
    ValidationChangeCountsOut,
    ValidationChangeOut,
    ValidationCompatibilityOut,
    ValidationFindingOut,
    ValidationLocationOut,
    ValidationRuleSetRefOut,
)

# The report lists at most this many changes; ``changes_total`` and ``changes_truncated`` say so.
COMPAT_CHANGES_CAP: Final = 200

SEMVER_BREAKING_CHANGE: Final = "semver-breaking-change"
COMPATIBILITY_UNAVAILABLE: Final = "compatibility-unavailable"

SEMVER_BREAKING_MESSAGE: Final = (
    "A breaking change needs a new major version. The smallest passing version is {version}."
)
COMPATIBILITY_UNAVAILABLE_MESSAGE: Final = (
    "The previous version could not be compared, so its compatibility cannot be checked."
)

# A declared or baseline version is shown only when it is plain version characters, so a hostile
# ``version`` value can never smuggle free text into a stored report.
_VERSION_TEXT: Final = re.compile(rf"[0-9A-Za-z.+-]{{1,{MAX_VERSION_CHARS}}}")
_STABLE_SEMVER: Final = re.compile(
    r"(?P<major>0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?P<pre>-[0-9A-Za-z.-]+)?"
    r"(?:\+[0-9A-Za-z.-]+)?"
)

Mode = Literal["save", "publish"]


@dataclass(frozen=True)
class BaselineRef:
    """Which version a run compared against."""

    version_id: uuid.UUID | None
    semver: str | None


@dataclass(frozen=True)
class Compatibility:
    """The section and the native findings (already ``blocking``) of one run."""

    section: ValidationCompatibilityOut
    findings: list[ValidationFindingOut]


def version_text(value: object) -> str | None:
    """``value`` when it is a plain version string, else ``None``."""
    if isinstance(value, str) and _VERSION_TEXT.fullmatch(value) is not None:
        return value
    return None


def floor_from_version(value: object) -> int | None:
    """The stable major floor of a previous document (on-demand): its major unless a prerelease."""
    if not isinstance(value, str):
        return None
    found = _STABLE_SEMVER.fullmatch(value)
    if found is None or found.group("pre") is not None:
        return None
    return int(found.group("major"))


def _change_out(change: Any) -> ValidationChangeOut:
    allowed = value_allowed(change.element_kind, change.attribute)
    return ValidationChangeOut(
        code=change.code,
        change_class=change.cls,
        path=change.path,
        pointer_before=change.pointer_before,
        pointer_after=change.pointer_after,
        attribute=change.attribute,
        before=_value(change.before) if allowed else None,
        after=_value(change.after) if allowed else None,
    )


def _value(value: Any) -> ChangeValue:
    # The classifier already caps and scalarises allow-listed values; a second look keeps the
    # report value-free even if a later classifier change forgets to.
    if isinstance(value, list):
        return [item if isinstance(item, str | int | float | bool) else None for item in value]
    return value if isinstance(value, str | int | float | bool) else None


def _native(
    rule_key: str, message: str, pointer: str, path: str, *, outcome: Literal["fail", "error"]
) -> ValidationFindingOut:
    return ValidationFindingOut(
        rule_set=ValidationRuleSetRefOut(
            id=None, name=None, folder_id=None, folder_kind=None, scope=None, version=None
        ),
        source=NATIVE_SOURCE,
        mode="enforce",
        enforce_at=None,
        rule_key=rule_key,
        severity="error",
        outcome=outcome,
        message=message,
        location=ValidationLocationOut(pointer=pointer, path=path),
        blocking=True,
        error_code=None,
    )


def _empty_counts() -> ValidationChangeCountsOut:
    return ValidationChangeCountsOut(breaking=0, warning=0, info=0)


def _section(
    status: Literal["compared", "no-baseline", "unavailable"],
    baseline: BaselineRef | None,
    declared: str | None,
    *,
    bump: Literal["major", "minor", "patch", "none"] | None,
    bump_ok: bool | None,
    result: DiffResult | None = None,
) -> ValidationCompatibilityOut:
    ref = (
        None
        if baseline is None
        else ValidationBaselineRefOut(
            version_id=baseline.version_id, semver=version_text(baseline.semver)
        )
    )
    if result is None:
        return ValidationCompatibilityOut(
            status=status,
            baseline=ref,
            classifier_version=None,
            declared_version=declared,
            required_bump=bump,
            bump_ok=bump_ok,
            counts=_empty_counts(),
            changes=[],
            changes_total=0,
            changes_truncated=False,
        )
    listed = result.changes[:COMPAT_CHANGES_CAP]
    return ValidationCompatibilityOut(
        status=status,
        baseline=ref,
        classifier_version=result.classifier_version,
        declared_version=declared,
        required_bump=bump,
        bump_ok=bump_ok,
        counts=ValidationChangeCountsOut(
            breaking=result.counts.get("breaking", 0),
            warning=result.counts.get("warning", 0),
            info=result.counts.get("info", 0),
        ),
        changes=[_change_out(change) for change in listed],
        changes_total=result.changes_total,
        changes_truncated=result.truncated or len(result.changes) > len(listed),
    )


def build_compatibility(
    *,
    mode: Mode,
    baseline: BaselineRef | None,
    result: DiffResult | None,
    declared: object,
    stable_major_floor: int | None,
) -> Compatibility:
    """The section and native findings for one run.

    ``baseline`` is ``None`` when the caller sent none. With a baseline, ``result`` ``None`` means
    it could not be used: publish mode then fails closed with a native blocking finding, save mode
    only reports ``unavailable``.
    """
    shown = version_text(declared)
    if baseline is None:
        return Compatibility(_section("no-baseline", None, shown, bump="none", bump_ok=True), [])
    if result is None:
        findings = (
            [
                _native(
                    COMPATIBILITY_UNAVAILABLE,
                    COMPATIBILITY_UNAVAILABLE_MESSAGE,
                    "",
                    "$",
                    outcome="error",
                )
            ]
            if mode == "publish"
            else []
        )
        return Compatibility(
            _section("unavailable", baseline, shown, bump=None, bump_ok=None), findings
        )
    verdict = required_bump(
        result,
        declared=declared if isinstance(declared, str) else "",
        stable_major_floor=stable_major_floor,
    )
    findings = []
    if not verdict.bump_ok:
        # not ok implies a floor exists, so the smallest passing version is always known
        smallest = verdict.smallest_passing or f"{(stable_major_floor or 0) + 1}.0.0"
        findings.append(
            _native(
                SEMVER_BREAKING_CHANGE,
                SEMVER_BREAKING_MESSAGE.format(version=smallest),
                "/version",
                "$.version",
                outcome="fail",
            )
        )
    return Compatibility(
        _section(
            "compared",
            baseline,
            shown,
            bump=verdict.required_bump,
            bump_ok=verdict.bump_ok,
            result=result,
        ),
        findings,
    )


__all__ = [
    "COMPAT_CHANGES_CAP",
    "COMPATIBILITY_UNAVAILABLE",
    "SEMVER_BREAKING_CHANGE",
    "BaselineRef",
    "Compatibility",
    "build_compatibility",
    "floor_from_version",
    "version_text",
]
