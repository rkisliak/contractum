"""The classified changelog between two versions the caller can read (D-156, VAL-09).

Computed on the fly with ``dc_odcs.diff`` and never stored. Each side is named by a ref: a strict
SemVer string or ``latest`` (resolved with the same visibility rules as the version fetch), or a
draft id, which only a caller holding ``contract:read`` at the contract's folder may name. Anything
the caller cannot see is the uniform 404, so a draft id or an unpublished version has no existence
oracle. ``from`` newer than ``to`` is simply compared in that order.

The classified list is deterministic (ordered by path, code and attribute), so paging is an offset
over it behind an opaque cursor, and the ETag depends only on the two content hashes and the
classifier version.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from dc_core.problems import ProblemError
from dc_odcs.diff import (
    CLASSIFIER_VERSION,
    MAX_DIFF_CHANGES,
    BumpVerdict,
    DiffResult,
    compare,
    required_bump,
)
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.drafts import get_draft
from dc_contracts.lifecycle import parts_of
from dc_contracts.models import Contract
from dc_contracts.versions import resolve_ref as resolve_version_ref
from dc_contracts.versions import visible_states

DEFAULT_DIFF_LIMIT = 500
MAX_DIFF_LIMIT = 1_000

_CURSOR_PREFIX = "o"
_MAX_CURSOR_CHARS = 32


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


@dataclass(frozen=True)
class DiffSide:
    """One resolved side of a diff: what the caller named and the parsed document it stands for."""

    ref: str
    kind: Literal["version", "draft"]
    version_id: uuid.UUID
    semver: str | None
    content_sha256: str
    document: Mapping[str, Any] = field(repr=False)


def _as_draft_id(ref: str) -> uuid.UUID | None:
    """The draft id a ref names, or None when it is not a UUID (SemVer and ``latest`` never are)."""
    try:
        return uuid.UUID(ref)
    except ValueError:
        return None


Refuse = Callable[[str], Awaitable[ProblemError]]


async def _plain_refusal(permission: str) -> ProblemError:
    return _not_found()


async def resolve_ref(
    session: AsyncSession,
    contract: Contract,
    ref: str,
    permissions: frozenset[str],
    refuse: Refuse = _plain_refusal,
) -> DiffSide:
    """The visible version or readable draft ``ref`` names, or the uniform 404.

    ``permissions`` are the caller's permissions at the contract's folder (the PDP decision of the
    route's policy), so the draft check is that decision, never a token claim. A caller who may
    only read published versions learns nothing of a contract without a Latest. A refusal that
    the missing permission decides is the problem ``refuse(permission)`` returns; the route
    passes ``dc_auth.deps.recorded_refusal`` so the PDP records it as a denial (WR-01).
    """
    can_read_drafts = "contract:read" in permissions
    if not can_read_drafts and contract.latest_version_id is None:
        raise await refuse("contract:read")
    draft_id = _as_draft_id(ref)
    if draft_id is not None:
        if not can_read_drafts:
            raise await refuse("contract:read")
        draft = await get_draft(session, contract.id, draft_id)
        if draft is None:
            raise _not_found()
        return DiffSide(
            ref=ref,
            kind="draft",
            version_id=draft.id,
            semver=draft.semver,
            content_sha256=draft.content_sha256,
            document=draft.document,
        )
    version = await resolve_version_ref(session, contract.id, ref)
    if version is None:
        raise _not_found()
    if version.state not in visible_states(permissions):
        raise await refuse("contract:publish")
    return DiffSide(
        ref=ref,
        kind="version",
        version_id=version.id,
        semver=parts_of(version).text,
        content_sha256=version.content_sha256,
        document=version.document,
    )


def diff_etag(
    from_sha256: str, to_sha256: str, classifier_version: str = CLASSIFIER_VERSION
) -> str:
    """The strong ETag of a diff: the two contents and the classifier decide the whole answer."""
    digest = hashlib.sha256(f"{from_sha256}:{to_sha256}:{classifier_version}".encode()).hexdigest()
    return f'"diff-{digest}"'


def encode_diff_cursor(offset: int) -> str:
    """Opaque cursor for the next page: base64url of the offset, without padding."""
    raw = f"{_CURSOR_PREFIX}{offset}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_diff_cursor(raw: str) -> int:
    """Inverse of :func:`encode_diff_cursor`; anything else (not canonical) is a 422."""
    try:
        if len(raw) > _MAX_CURSOR_CHARS:
            raise ValueError("cursor too long")
        padded = raw + "=" * (-len(raw) % 4)
        text = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True).decode(
            "ascii"
        )
        if not text.startswith(_CURSOR_PREFIX) or not text[1:].isdecimal():
            raise ValueError("not a diff cursor")
        offset = int(text[1:])
        if encode_diff_cursor(offset) != raw:
            raise ValueError("not the canonical cursor")
    except (ValueError, binascii.Error, UnicodeError):
        raise ProblemError(
            422,
            "validation-error",
            "Validation error",
            "The request did not validate.",
            errors=[{"pointer": "/query/cursor", "detail": "cursor is not valid"}],
        ) from None
    return offset


def compute(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[DiffResult, BumpVerdict]:
    """Classify ``before`` -> ``after`` and the bump the changes call for (CPU work: use a thread).

    ``declared`` is the ``to`` document's own version and there is no floor, so ``required_bump``
    reports the class-based bump (major for breaking, minor for warnings, patch for info only).
    """
    result = compare(before, after, max_changes=MAX_DIFF_CHANGES)
    declared = after.get("version")
    verdict = required_bump(
        result, declared=declared if isinstance(declared, str) else "", stable_major_floor=None
    )
    return result, verdict


@dataclass(frozen=True)
class DiffPage:
    """One page of a computed diff."""

    classifier_version: str
    required_bump: Literal["major", "minor", "patch", "none"]
    counts: dict[str, int]
    changes: list[dict[str, Any]]
    changes_total: int
    truncated: bool
    next_cursor: str | None


def diff_page(result: DiffResult, verdict: BumpVerdict, offset: int, limit: int) -> DiffPage:
    """At most ``limit`` changes from ``offset`` of the deterministic list, and the next cursor."""
    end = offset + limit
    return DiffPage(
        classifier_version=result.classifier_version,
        required_bump=verdict.required_bump,
        counts={key: int(value) for key, value in result.counts.items()},
        changes=[change.to_json() for change in result.changes[offset:end]],
        changes_total=result.changes_total,
        truncated=result.truncated,
        next_cursor=encode_diff_cursor(end) if end < len(result.changes) else None,
    )


__all__ = [
    "DEFAULT_DIFF_LIMIT",
    "MAX_DIFF_LIMIT",
    "DiffPage",
    "DiffSide",
    "compute",
    "decode_diff_cursor",
    "diff_etag",
    "diff_page",
    "encode_diff_cursor",
    "resolve_ref",
]
