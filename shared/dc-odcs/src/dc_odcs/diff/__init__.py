"""Breaking-change classification of two ODCS documents (D-144 to D-148).

Pure and stdlib-only: normalise (pair keyed collections), walk both documents and classify each
difference through the rule table. Bump ``CLASSIFIER_VERSION`` whenever a rule row changes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from dc_odcs.diff.model import (
    ALLOWED_VALUE_ATTRIBUTES,
    MAX_DIFF_CHANGES,
    Change,
    DiffResult,
)
from dc_odcs.diff.walk import walk

CLASSIFIER_VERSION: Final = "odcs-compat-1"

_IDENT = r"(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
# SemVer 2.0.0 (https://semver.org), the official regular expression with named major.
_SEMVER: Final = re.compile(
    r"(?P<major>0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    rf"(?:-{_IDENT}(?:\.{_IDENT})*)?"
    r"(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?"
)


def compare(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any],
    *,
    max_changes: int = MAX_DIFF_CHANGES,
) -> DiffResult:
    """Classify every difference between ``before`` and ``after`` (no baseline: no changes).

    At most ``max_changes`` changes are returned (``truncated`` then ``True``); ``counts`` and
    ``changes_total`` always carry the true totals.
    """
    if before is None:
        return DiffResult(
            (), {"breaking": 0, "warning": 0, "info": 0}, 0, False, CLASSIFIER_VERSION
        )
    sink = walk(dict(before), dict(after), max_changes=max_changes)
    changes = sorted(sink.changes, key=lambda c: (c.path, c.code, c.attribute or ""))
    return DiffResult(
        tuple(changes),
        dict(sink.counts),
        sink.total,
        sink.total > len(changes),
        CLASSIFIER_VERSION,
    )


@dataclass(frozen=True)
class BumpVerdict:
    """The semver verdict of a comparison (D-146, D-148)."""

    status: Literal["compared", "no-baseline"]
    required_bump: Literal["major", "minor", "patch", "none"]
    bump_ok: bool
    smallest_passing: str | None


def _major(declared: str) -> int | None:
    found = _SEMVER.fullmatch(declared)
    return int(found.group("major")) if found is not None else None


def required_bump(
    result: DiffResult | None, *, declared: str, stable_major_floor: int | None
) -> BumpVerdict:
    """What the changes require and whether ``declared`` satisfies the enforced rule.

    Only the breaking-needs-major rule is enforced: a breaking change is publishable only when
    the declared major is above ``stable_major_floor`` (the highest stable major published; ``None``
    when no stable release exists yet). ``0.x`` is not special. Warnings and info changes only
    advise a minor or patch bump.
    """
    if result is None:
        return BumpVerdict("no-baseline", "none", True, None)
    if result.counts.get("breaking", 0) > 0:
        major = _major(declared)
        ok = stable_major_floor is None or (major is not None and major > stable_major_floor)
        smallest = None if ok or stable_major_floor is None else f"{stable_major_floor + 1}.0.0"
        return BumpVerdict("compared", "major", ok, smallest)
    if result.counts.get("warning", 0) > 0:
        return BumpVerdict("compared", "minor", True, None)
    if result.counts.get("info", 0) > 0:
        return BumpVerdict("compared", "patch", True, None)
    return BumpVerdict("compared", "none", True, None)


__all__ = [
    "ALLOWED_VALUE_ATTRIBUTES",
    "CLASSIFIER_VERSION",
    "MAX_DIFF_CHANGES",
    "BumpVerdict",
    "Change",
    "DiffResult",
    "compare",
    "required_bump",
]
