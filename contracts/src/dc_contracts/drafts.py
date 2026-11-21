"""Draft domain service: lock, save, read and list contract drafts (CONT-03, D-42).

A draft is a ``ContractVersion`` row in state ``draft``. Saves are optimistic-concurrency
protected: the row is locked ``FOR UPDATE`` (with ``populate_existing`` so a stale identity-map
copy never hides a concurrent writer's revision, research Pitfall 1), the caller's ``If-Match``
is compared with the locked revision's ETag, and an accepted save bumps the revision.

Every write goes through the caller's ``UnitOfWork`` and records exactly one metadata-only audit
event (never document content). Callers authorise first.
"""

from __future__ import annotations

import uuid
from typing import Any

from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from dc_odcs import ParsedContract, SearchFields
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.http_cache import etag_for_draft, require_if_match
from dc_contracts.ingest import check_document_name
from dc_contracts.models import Contract, ContractVersion

# Free text is bounded in events so one event can never outgrow the NATS max_payload (WR-01).
AUDIT_NAME_MAX_CHARS = 200


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


def draft_snapshot(version: ContractVersion) -> dict[str, Any]:
    """Allow-listed audit view of a draft; the name is bounded, documents are never included."""
    snapshot = UnitOfWork.snapshot(version)
    snapshot["name"] = version.name[:AUDIT_NAME_MAX_CHARS] if version.name is not None else None
    return snapshot


async def lock_draft(
    session: AsyncSession, contract_id: uuid.UUID, draft_id: uuid.UUID
) -> ContractVersion:
    """The draft row under ``SELECT ... FOR UPDATE``.

    404 unless the row is this contract's; 409 ``draft-already-published`` when the row is no
    longer a draft, so a published draft id is read-only and a stale ``If-Match`` on it never
    answers 412 (D-51). The problem carries only the version id.
    """
    draft = (
        await session.execute(
            select(ContractVersion)
            .where(ContractVersion.id == draft_id, ContractVersion.contract_id == contract_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if draft is None:
        raise _not_found()
    if draft.state != "draft":
        raise ProblemError(
            409,
            "draft-already-published",
            "Conflict",
            "This draft has been published; it is read-only now.",
            version_id=str(draft.id),
        )
    return draft


def odcs_id_mismatch(expected: str, found: str) -> ProblemError:
    """The 422 for a document whose ``id`` is not the contract's ODCS id.

    Names both ids as the ``expected_odcs_id`` and ``found_odcs_id`` members. Neither is secret:
    the caller already reads the contract, and the found id is the one they just sent (bounded by
    the ingest gate).
    """
    return ProblemError(
        422,
        "odcs-id-mismatch",
        "Unprocessable Content",
        "The document id must stay the contract's ODCS id. "
        f"Expected {expected!r}, found {found!r}.",
        expected_odcs_id=expected,
        found_odcs_id=found,
    )


async def save_draft(
    uow: UnitOfWork,
    contract: Contract,
    draft_id: uuid.UUID,
    parsed: ParsedContract,
    raw: bytes,
    fields: SearchFields,
    if_match: str | None,
) -> tuple[ContractVersion, bool]:
    """Replace the draft bytes; ``bool`` is True when anything changed.

    Order under the row lock: ``If-Match`` (428/412), the ODCS id must stay the contract's
    (422), a changed top-level name must pass the name rule (422), then a save of identical
    bytes and media type is a no-op (same revision, no event).
    """
    session = uow.session
    draft = await lock_draft(session, contract.id, draft_id)
    require_if_match(if_match, etag_for_draft(draft.revision))
    if parsed.odcs_id != contract.odcs_id:
        raise odcs_id_mismatch(contract.odcs_id, parsed.odcs_id)
    # A rename is a new write (quick 261120-lbz, D-01); a document that keeps the draft's stored
    # name is never re-checked, so a legacy name does not block an unrelated save.
    check_document_name(parsed.document, draft.name)
    if draft.content_sha256 == parsed.content_sha256 and draft.media_type == parsed.media_type:
        return draft, False

    before = draft_snapshot(draft)
    draft.raw_document = raw
    draft.media_type = parsed.media_type
    draft.content_sha256 = parsed.content_sha256
    draft.size_bytes = parsed.size_bytes
    draft.document = parsed.document
    draft.odcs_api_version = parsed.api_version
    draft.name = fields.name
    draft.domain = fields.domain
    draft.status = fields.status
    draft.tags = list(fields.tags)
    draft.description_text = fields.description_text
    draft.odcs_version = fields.odcs_version
    draft.revision = draft.revision + 1
    await session.flush()
    await session.refresh(draft)
    uow.audit(
        "contract.draft_updated",
        "contract",
        contract.id,
        before=before,
        after=draft_snapshot(draft),
        scope_path=contract.ancestor_ids,
        aggregate_version=draft.revision,
    )
    return draft, True


async def get_draft(
    session: AsyncSession, contract_id: uuid.UUID, draft_id: uuid.UUID
) -> ContractVersion | None:
    """One draft of the contract (no lock), or None."""
    return (
        await session.execute(
            select(ContractVersion).where(
                ContractVersion.id == draft_id,
                ContractVersion.contract_id == contract_id,
                ContractVersion.state == "draft",
            )
        )
    ).scalar_one_or_none()


async def list_drafts(session: AsyncSession, contract_id: uuid.UUID) -> list[ContractVersion]:
    """The contract's drafts, oldest version number first."""
    return list(
        (
            await session.execute(
                select(ContractVersion)
                .where(ContractVersion.contract_id == contract_id, ContractVersion.state == "draft")
                .order_by(ContractVersion.version_no.asc())
            )
        )
        .scalars()
        .all()
    )


__all__ = [
    "draft_snapshot",
    "get_draft",
    "list_drafts",
    "lock_draft",
    "save_draft",
]
