"""Mandatory rule-set resolution: a pure function with an injected clock (D-120, D-121, D-142).

No I/O and no wall-clock read in this module: the caller passes ``now`` (one database ``SELECT
now()`` per run), so every rule here is testable at an exact instant.

Mode is computed, never scheduled. A published version whose ``enforce_at`` has passed (``<= now``)
is ``enforce``; one whose ``enforce_at`` is still ahead is ``warn``; one with no ``enforce_at`` does
not resolve at all. Per mandatory rule set:

* the **enforced** version is the newest version with ``enforce_at <= now``; it can block;
* every version **newer** than the enforced one (every version when none is enforced) whose
  ``enforce_at`` is still in the future is reported too, each as its own non-blocking ``warn``
  entry, so no intermediate version of a rollout is ever skipped;
* an older version is superseded once a newer one is enforced (it drops out).

A **paused** set (platform sets only, a Global admin's call: quick 261118-vpa) resolves the same
versions, but every entry is a non-blocking ``warn`` carrying ``paused=True``: its rules still run
and report, they just never block. The pause is an overlay: ``enforce_at`` is untouched, so resuming
needs no restamp (the mode is computed from ``enforce_at`` again) and ``enforced_version_no`` stays
the pure date answer.

A deprecated version is not filtered here: deprecation is an attach-time gate only, so the enforced
version keeps resolving until a newer version is enforced.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

Mode = Literal["enforce", "warn"]
Source = Literal["mandatory", "explicit", "adhoc"]


@dataclass(frozen=True)
class VersionInfo:
    """One published version as resolution sees it (no rules: those are loaded when evaluated)."""

    id: uuid.UUID
    version_no: int
    dsl_version: str
    enforce_at: datetime | None
    deprecated_at: datetime | None


@dataclass(frozen=True)
class CandidateSet:
    """A mandatory rule set held by the platform (depth 0) or a folder (depth 1 = ROOT, ...)."""

    rule_set_id: uuid.UUID
    name: str
    scope: str
    folder_id: uuid.UUID | None
    folder_kind: str | None
    depth: int
    versions: tuple[VersionInfo, ...]
    paused: bool = False


@dataclass(frozen=True)
class ResolvedRuleSet:
    """One rule-set version that applies to a contract, and in which mode."""

    rule_set_id: uuid.UUID
    rule_set_version_id: uuid.UUID
    name: str
    scope: str
    folder_id: uuid.UUID | None
    folder_kind: str | None
    depth: int
    version_no: int
    dsl_version: str
    source: Source
    mode: Mode
    enforce_at: datetime | None
    paused: bool = False


@dataclass(frozen=True)
class Attachment:
    """An explicit attachment as resolution sees it: a rule set and a version spec."""

    rule_set_id: uuid.UUID
    version: int | Literal["latest"]


@dataclass(frozen=True)
class UnresolvedAttachment:
    """An attachment that could not be resolved: it blocks and is never skipped."""

    rule_set_id: uuid.UUID
    version_spec: str
    source: Source


def enforced_version_no(candidate: CandidateSet, now: datetime) -> int | None:
    """The version ``resolve_mandatory`` would enforce for this set at ``now``, else None."""
    passed = [
        v.version_no for v in candidate.versions if v.enforce_at is not None and v.enforce_at <= now
    ]
    return max(passed) if passed else None


def order_key(resolved: ResolvedRuleSet) -> tuple[int, str, str, int, int]:
    """Order: holder depth, lower-cased name, rule-set id, enforce before warn, version."""
    return (
        resolved.depth,
        resolved.name.lower(),
        str(resolved.rule_set_id),
        0 if resolved.mode == "enforce" else 1,
        resolved.version_no,
    )


def _entry(candidate: CandidateSet, version: VersionInfo, mode: Mode) -> ResolvedRuleSet:
    paused = candidate.paused
    return ResolvedRuleSet(
        rule_set_id=candidate.rule_set_id,
        rule_set_version_id=version.id,
        name=candidate.name,
        scope=candidate.scope,
        folder_id=candidate.folder_id,
        folder_kind=candidate.folder_kind,
        depth=candidate.depth,
        version_no=version.version_no,
        dsl_version=version.dsl_version,
        source="mandatory",
        mode="warn" if paused else mode,
        enforce_at=version.enforce_at,
        paused=paused,
    )


def resolve_mandatory(sets: Sequence[CandidateSet], now: datetime) -> list[ResolvedRuleSet]:
    """The mandatory entries that apply, ordered by ``order_key`` whatever the input order.

    The same rule set given twice resolves once (D-122: a set reached by two routes is reported as
    mandatory a single time).
    """
    seen: set[uuid.UUID] = set()
    resolved: list[ResolvedRuleSet] = []
    for candidate in sets:
        if candidate.rule_set_id in seen:
            continue
        seen.add(candidate.rule_set_id)
        versions = sorted(candidate.versions, key=lambda v: v.version_no)
        enforced = enforced_version_no(candidate, now)
        for version in versions:
            if version.enforce_at is None:
                continue
            if version.version_no == enforced:
                resolved.append(_entry(candidate, version, "enforce"))
            elif version.enforce_at > now and (enforced is None or version.version_no > enforced):
                resolved.append(_entry(candidate, version, "warn"))
    return sorted(resolved, key=order_key)


def restamp_on_set(
    versions: Sequence[VersionInfo],
    restore_version_no: int | None,
    now: datetime,
    grace: timedelta,
) -> dict[int, datetime]:
    """The ``version_no -> enforce_at`` map that setting the mandatory flag writes (D-120, D-142).

    With ``restore_version_no`` n (the version enforced when the flag was last cleared) version n is
    enforced again immediately and every published version newer than n gets a new grace period;
    older versions are left alone. Without it only the newest version is stamped (a first-time set).
    A governance rollout is never lowered by clear-then-set: n is enforced at ``now``, not warned.
    """
    numbers = sorted(v.version_no for v in versions)
    if not numbers:
        return {}
    if restore_version_no is None or restore_version_no not in numbers:
        return {numbers[-1]: now + grace}
    stamps: dict[int, datetime] = {restore_version_no: now}
    for number in numbers:
        if number > restore_version_no:
            stamps[number] = now + grace
    return stamps


def _visible(candidate: CandidateSet, ancestors: Collection[uuid.UUID]) -> bool:
    """Held by the platform or by the folder or one of its ancestors, and ever published."""
    held_here = candidate.scope == "platform" or (
        candidate.folder_id is not None and candidate.folder_id in ancestors
    )
    return held_here and bool(candidate.versions)


def _pick(candidate: CandidateSet, spec: int | Literal["latest"]) -> VersionInfo | None:
    """``latest`` is the newest non-deprecated version (the newest when all are deprecated, D-123);
    an exact number is that version whether or not it is deprecated."""
    if spec == "latest":
        active = [v for v in candidate.versions if v.deprecated_at is None]
        pool = active or list(candidate.versions)
        return max(pool, key=lambda v: v.version_no)
    return next((v for v in candidate.versions if v.version_no == spec), None)


def resolve_explicit(
    attachments: Sequence[Attachment],
    candidates_by_id: Mapping[uuid.UUID, CandidateSet],
    facts_ancestor_ids: Collection[uuid.UUID],
    mandatory_ids: Collection[uuid.UUID],
    source: Source,
) -> tuple[list[ResolvedRuleSet], list[UnresolvedAttachment]]:
    """Resolve attachments (``source`` is ``explicit`` or ``adhoc``) into enforced entries.

    A set is visible when the platform or the folder or an ancestor holds it and it has a published
    version; ``latest`` and exact versions follow D-122 and D-123. An attachment that cannot resolve
    (unknown, not visible, never published, missing exact version) is returned as unresolved, never
    dropped: it blocks. A set that is already mandatory for the folder resolves once, as
    mandatory, so it is skipped here. Repeats collapse: the same version resolves once and the same
    unresolved attachment is reported once. Entries come back in ``order_key`` order whatever the
    attachment order.
    """
    mandatory = set(mandatory_ids)
    resolved: dict[uuid.UUID, ResolvedRuleSet] = {}
    unresolved: dict[tuple[uuid.UUID, str], UnresolvedAttachment] = {}
    for attachment in attachments:
        if attachment.rule_set_id in mandatory:
            continue
        spec = "latest" if attachment.version == "latest" else str(attachment.version)
        candidate = candidates_by_id.get(attachment.rule_set_id)
        version = (
            _pick(candidate, attachment.version)
            if candidate is not None and _visible(candidate, facts_ancestor_ids)
            else None
        )
        if candidate is None or version is None:
            unresolved.setdefault(
                (attachment.rule_set_id, spec),
                UnresolvedAttachment(
                    rule_set_id=attachment.rule_set_id, version_spec=spec, source=source
                ),
            )
            continue
        resolved.setdefault(
            version.id,
            ResolvedRuleSet(
                rule_set_id=candidate.rule_set_id,
                rule_set_version_id=version.id,
                name=candidate.name,
                scope=candidate.scope,
                folder_id=candidate.folder_id,
                folder_kind=candidate.folder_kind,
                depth=candidate.depth,
                version_no=version.version_no,
                dsl_version=version.dsl_version,
                source=source,
                mode="enforce",
                enforce_at=None,
            ),
        )
    return sorted(resolved.values(), key=order_key), list(unresolved.values())
