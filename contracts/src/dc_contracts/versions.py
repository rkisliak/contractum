"""Version domain service: publish a draft as the next immutable version (VER-01, VER-03, VER-08).

A draft is published in place: the ``ContractVersion`` row keeps its id and its stored bytes and
changes state to ``published``; the first publish of a contract also becomes the Latest
(``contract.latest_version_id`` plus ``is_latest`` on the row, D-39, D-51).

Lock order is always the contract row first (``FOR NO KEY UPDATE``), then the draft row
(``FOR UPDATE``); every lifecycle write takes the same order, so two of them can never deadlock.
The publish gate runs on the stored bytes before any lock is taken (the real gate calls the
validation service, and a row lock must not be held across a network call). Under the locks the
draft's revision must still equal ``If-Match`` and its hash the hash that was gated, so the bytes
that are published are provably the bytes that were checked (VER-08, D-50).

Writes go through the caller's ``UnitOfWork`` and record exactly one metadata-only audit event.
Callers authorise first.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from dc_odcs import ParsedContract, extract_search_fields
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.attachments import attachments_for_gate
from dc_contracts.compat import baseline_and_floor, baseline_key
from dc_contracts.drafts import draft_snapshot, get_draft, lock_draft, odcs_id_mismatch
from dc_contracts.http_cache import etag_for_draft, require_if_match
from dc_contracts.ingest import parse_upload
from dc_contracts.lifecycle import (
    BumpKind,
    SemverParts,
    VersionStatus,
    bump,
    derive_status,
    parse_semver,
    parts_of,
    validate_publish,
)
from dc_contracts.link_facts import LinkFacts, recheck_link_facts
from dc_contracts.locks import lock_contract
from dc_contracts.models import Contract, ContractVersion
from dc_contracts.notation import get_adapter
from dc_contracts.notices import audience_for_contract, audience_for_version, pins_for_contract
from dc_contracts.pins import pinned_card_out, pinned_cards_for_contract, pinned_cards_for_version
from dc_contracts.references import (
    Referrer,
    blocking_referrers,
    deleted_link_targets,
    index_references,
)
from dc_contracts.settings import ContractsSettings
from dc_contracts.validation_client import FailClosedGate, GateContext, GateResult


class PublishGate(Protocol):
    """Checks the exact stored bytes before they become a published version (D-131)."""

    async def check(
        self, raw: bytes, media_type: str, settings: ContractsSettings, ctx: GateContext
    ) -> GateResult: ...


class PassThroughPublishGate:
    """Only the D-19 ODCS acceptance check, no governance. **A test double**: no app ever installs
    it. Production installs ``ValidationPublishGate`` (``validation_hook``); with no gate at all
    ``get_publish_gate`` falls back to the fail-closed gate, never to this one."""

    async def check(
        self, raw: bytes, media_type: str, settings: ContractsSettings, ctx: GateContext
    ) -> GateResult:
        parsed = await parse_upload(raw, media_type, settings)
        return GateResult(
            parsed=parsed,
            run_id=None,
            pins=[],
            engine_version=None,
            schema_sha=None,
            attachments_revision=ctx.attachments_revision,
            baseline_id=ctx.baseline.version_id if ctx.baseline is not None else None,
            stable_major_floor=ctx.stable_major_floor,
        )


_DEFAULT_GATE = FailClosedGate()


def get_publish_gate(state: Any) -> PublishGate:
    """The gate the app installed, else the fail-closed one (503 ``validation-unavailable``)."""
    gate: PublishGate | None = getattr(state, "publish_gate", None)
    return gate if gate is not None else _DEFAULT_GATE


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


async def get_version_by_semver(
    session: AsyncSession, contract_id: uuid.UUID, ref: str, *, states: frozenset[str]
) -> ContractVersion | None:
    """The non-draft version of the contract with this strict SemVer in one of ``states``.

    None for a reference that is not strict SemVer, an unknown version or a state the caller may
    not see. Drafts are never returned (they are addressed by id, through the drafts routes).
    """
    parts = parse_semver(ref)
    if parts is None:
        return None
    return (
        await session.execute(
            select(ContractVersion).where(
                ContractVersion.contract_id == contract_id,
                ContractVersion.state.in_(sorted(states - {"draft"})),
                ContractVersion.semver_major == parts.major,
                ContractVersion.semver_minor == parts.minor,
                ContractVersion.semver_patch == parts.patch,
                ContractVersion.semver_prerelease == parts.prerelease,
            )
        )
    ).scalar_one_or_none()


async def semver_rows(session: AsyncSession, contract_id: uuid.UUID) -> list[SemverParts]:
    """The parsed version of every non-draft row (published and unpublished) of the contract."""
    rows = (
        (
            await session.execute(
                select(ContractVersion).where(
                    ContractVersion.contract_id == contract_id,
                    ContractVersion.state != "draft",
                )
            )
        )
        .scalars()
        .all()
    )
    return [parts_of(row) for row in rows]


async def resolve_ref(
    session: AsyncSession, contract_id: uuid.UUID, ref: str
) -> ContractVersion | None:
    """The non-draft version a URL reference names, or None.

    ``latest`` is resolved in one statement through ``contract.latest_version_id`` so a concurrent
    promote can never tear the read (D-48). Anything else must be strict SemVer; the lookup uses
    the typed parts, so build metadata in the reference is ignored (matching the equality rule).
    A draft is never returned.
    """
    if ref == "latest":
        stmt = (
            select(ContractVersion)
            .join(Contract, Contract.latest_version_id == ContractVersion.id)
            .where(Contract.id == contract_id, ContractVersion.contract_id == contract_id)
        )
    else:
        parts = parse_semver(ref)
        if parts is None:
            return None
        stmt = select(ContractVersion).where(
            ContractVersion.contract_id == contract_id,
            ContractVersion.state != "draft",
            ContractVersion.semver_major == parts.major,
            ContractVersion.semver_minor == parts.minor,
            ContractVersion.semver_patch == parts.patch,
            ContractVersion.semver_prerelease == parts.prerelease,
        )
    return (await session.execute(stmt)).scalar_one_or_none()


def visible_states(permissions: frozenset[str]) -> frozenset[str]:
    """The row states a version read route may return to a caller holding ``permissions``.

    Everyone with ``contract:read-published`` sees ``published`` rows. Holders of
    ``contract:publish`` (the owners) also see ``unpublished`` ones, because they must be able to
    find an unpublished version to republish it (A3). A draft is never a version read.
    """
    if "contract:publish" in permissions:
        return frozenset({"published", "unpublished"})
    return frozenset({"published"})


async def read_contract(session: AsyncSession, contract_id: uuid.UUID) -> Contract:
    """The contract row (unlocked read); 404 when absent."""
    contract = (
        await session.execute(select(Contract).where(Contract.id == contract_id))
    ).scalar_one_or_none()
    if contract is None:
        raise _not_found()
    return contract


async def list_versions(
    session: AsyncSession, contract_id: uuid.UUID, *, states: frozenset[str]
) -> tuple[Contract, list[ContractVersion]]:
    """The contract and its non-draft rows whose state is in ``states`` (unordered); 404 if absent.

    Drafts are excluded by construction: ``states`` is only ever intersected with the non-draft
    states, so a caller can never be handed one through this function.
    """
    contract = await read_contract(session, contract_id)
    wanted = sorted(states - {"draft"})
    rows = (
        (
            await session.execute(
                select(ContractVersion).where(
                    ContractVersion.contract_id == contract_id,
                    ContractVersion.state.in_(wanted),
                )
            )
        )
        .scalars()
        .all()
    )
    return contract, list(rows)


async def latest_parts(session: AsyncSession, contract: Contract) -> SemverParts | None:
    """The parsed version of the contract's Latest, or None when it has none."""
    if contract.latest_version_id is None:
        return None
    row = (
        await session.execute(
            select(ContractVersion).where(
                ContractVersion.id == contract.latest_version_id,
                ContractVersion.contract_id == contract.id,
            )
        )
    ).scalar_one_or_none()
    return None if row is None else parts_of(row)


def version_snapshot(version: ContractVersion) -> dict[str, Any]:
    """Allow-listed audit view of a version row; the name is bounded, documents never included."""
    return draft_snapshot(version)


def contract_snapshot(contract: Contract) -> dict[str, Any]:
    """Allow-listed audit view of a contract row (metadata only, never document content)."""
    return UnitOfWork.snapshot(contract)


REASON_LATEST = "latest-version-protected"
REASON_REFERENCED = "contract-referenced"
REASON_VERSION_PINNED = "version-pinned"
REASON_CONTRACT_PINNED = "contract-pinned"


class LifecycleBlocked(ProblemError):
    """A lifecycle write refused for one or more reasons, all listed in ``reasons`` (D-106).

    The problem type is the first reason's code, so a client that only knows the Phase 3 slugs
    still sees them. ``pinned_card_ids`` are the cards an unpublish refusal should tell; it stays
    empty for a refused delete, which records no event and no notice (D-92).
    """

    def __init__(
        self,
        reasons: list[dict[str, Any]],
        *,
        detail: str,
        version_id: uuid.UUID | None = None,
        semver: str | None = None,
        pinned_card_ids: tuple[uuid.UUID, ...] = (),
        **extensions: Any,
    ) -> None:
        super().__init__(409, reasons[0]["code"], "Conflict", detail, reasons=reasons, **extensions)
        self.reasons = reasons
        self.version_id = version_id
        self.semver = semver
        self.pinned_card_ids = pinned_card_ids


def _links_to_deleted(count: int, *, detail: str) -> LifecycleBlocked:
    """The reference guard seen from the referrer's side (WR-03): 409 ``contract-referenced``.

    Deleted targets are hidden from everyone, so they are only counted, never named.
    """
    return LifecycleBlocked(
        [{"code": REASON_REFERENCED, "deleted_target_count": count}],
        detail=detail,
        deleted_target_count=count,
    )


async def delete_contract(
    uow: UnitOfWork, contract_id: uuid.UUID, *, visible: Callable[[Referrer], bool]
) -> None:
    """Soft-delete a contract: hidden from everyone, every row and the ODCS id stay (D-55).

    The contract row is locked ``FOR UPDATE`` (research Pattern 8), the strongest row lock, and
    the reference guard runs between that lock and the update: a publish that links to this
    contract holds ``FOR KEY SHARE`` on it, so it either finished before the lock (and is seen) or
    waits behind it. A published version of any other non-deleted contract that links to this one,
    in any Organization, refuses the delete with 409 ``contract-referenced`` (D-54); the refusal
    names only the referrers ``visible`` accepts and counts the rest, and never carries a folder,
    path or Organization. A card with an active pin on any version of the contract refuses the
    delete too (409 ``contract-pinned``, D-92, D-95); when both guards apply the 409 lists both
    reasons (D-106), records no event and notifies nobody. A deleted or unknown contract is the
    unknown-contract 404. Only
    ``deleted_at`` and ``deleted_by`` change: no version row is touched, so history and published
    bytes stay as they were. Records one metadata-only ``contract.deleted`` event.
    """
    session = uow.session
    contract = await lock_contract(session, contract_id, mode="update")
    reasons: list[dict[str, Any]] = []
    extensions: dict[str, Any] = {}
    referrers = await blocking_referrers(session, contract)
    if referrers:
        named = [r for r in referrers if visible(r)]
        listed = [
            {"contract_id": str(r.contract_id), "odcs_id": r.odcs_id}
            for r in sorted(named, key=lambda r: r.contract_id)
        ]
        hidden = len(referrers) - len(named)
        reasons.append(
            {"code": REASON_REFERENCED, "referrers": listed, "hidden_referrer_count": hidden}
        )
        extensions = {"referrers": listed, "hidden_referrer_count": hidden}
    # Under the same contract lock a subscribe takes, and before any write: a pin either committed
    # first (and is seen here) or the subscribe waits and then finds the contract deleted.
    pinned = await pinned_cards_for_contract(session, contract.id)
    if pinned:
        reasons.append(
            {"code": REASON_CONTRACT_PINNED, "cards": [pinned_card_out(c) for c in pinned]}
        )
    if reasons:
        raise LifecycleBlocked(
            reasons,
            detail=(
                "Another contract's published version references this contract; remove that "
                "reference first."
                if referrers
                else "Cards are pinned to versions of this contract; remove their "
                "subscriptions first."
            ),
            **extensions,
        )
    before = contract_snapshot(contract)
    contract.deleted_at = func.now()
    contract.deleted_by = uow.actor.id
    await session.flush()
    await session.refresh(contract)
    uow.audit(
        "contract.deleted",
        "contract",
        contract.id,
        before=before,
        after=contract_snapshot(contract),
        scope_path=contract.ancestor_ids,
        aggregate_version=contract.version_counter,
    )


async def restore_contract(uow: UnitOfWork, contract_id: uuid.UUID) -> tuple[Contract, bool]:
    """Bring a soft-deleted contract back (D-56); returns ``(contract, changed)``.

    Locks the row ``FOR UPDATE`` and finds it even when deleted. A contract that is not deleted is
    returned unchanged with ``False``, before the entity is touched and without an event. Otherwise
    both soft-delete columns are cleared and one metadata-only ``contract.restored`` event is
    recorded. An unknown id is the unknown-contract 404. A contract whose published versions link
    to a soft-deleted contract is refused with 409 ``contract-referenced`` (WR-03), counted only.
    """
    session = uow.session
    contract = await lock_contract(session, contract_id, mode="update", include_deleted=True)
    if contract.deleted_at is None:
        return contract, False
    # WR-03: while this contract was deleted its published links blocked nothing, so a target may
    # have been deleted since; restoring would bring a published link to a deleted contract back.
    published_ids = (
        (
            await session.execute(
                select(ContractVersion.id).where(
                    ContractVersion.contract_id == contract.id,
                    ContractVersion.state == "published",
                )
            )
        )
        .scalars()
        .all()
    )
    dangling = await deleted_link_targets(session, contract, list(published_ids))
    if dangling:
        raise _links_to_deleted(
            dangling,
            detail=(
                "A published version of this contract links to a deleted contract; restore that "
                "contract first."
            ),
        )
    before = contract_snapshot(contract)
    contract.deleted_at = None
    contract.deleted_by = None
    await session.flush()
    await session.refresh(contract)
    uow.audit(
        "contract.restored",
        "contract",
        contract.id,
        before=before,
        after=contract_snapshot(contract),
        scope_path=contract.ancestor_ids,
        aggregate_version=contract.version_counter,
    )
    return contract, True


@dataclass(frozen=True)
class PublishResult:
    version: ContractVersion
    status: VersionStatus


async def publish_draft(
    uow: UnitOfWork,
    contract_id: uuid.UUID,
    draft_id: uuid.UUID,
    if_match: str | None,
    settings: ContractsSettings,
    gate: PublishGate,
    ctx: GateContext,
) -> PublishResult:
    """Publish ``draft_id`` as an immutable version of the contract (one audit event)."""
    session = uow.session
    # (a) Unlocked read of plain columns (not the entity: an entity here would sit in the identity
    # map and hide a concurrent change from the locked read, Pitfall 1). No state filter.
    row = (
        await session.execute(
            select(
                ContractVersion.state,
                ContractVersion.revision,
                ContractVersion.raw_document,
                ContractVersion.media_type,
            ).where(ContractVersion.id == draft_id, ContractVersion.contract_id == contract_id)
        )
    ).first()
    if row is None:
        raise _not_found()
    contract_notation = (
        await session.execute(select(Contract.notation).where(Contract.id == contract_id))
    ).scalar_one_or_none()
    if contract_notation is None:
        raise _not_found()

    parsed: ParsedContract | None = None
    verdict: GateResult | None = None
    link_facts: LinkFacts | None = None
    if row.state == "draft":
        # (b) cheap early If-Match, (c) the gate on exactly these bytes, before any lock.
        require_if_match(if_match, etag_for_draft(row.revision))
        # D-124: the explicit attachments and their revision, read unlocked just before the gate.
        attachments, attachments_revision = await attachments_for_gate(session, contract_id)
        # D-146, D-147: the baseline (id, version, bytes) and the stable major floor, read in the
        # same unlocked block, so no row lock is held across the validation call.
        baseline = await baseline_and_floor(session, contract_id)
        # D-153: the caller's view of the document's contract links, read in the same block.
        link_facts = await ctx.link_resolver(session) if ctx.link_resolver is not None else None
        ref_facts = link_facts.facts if link_facts is not None else None
        ctx = dataclasses.replace(
            ctx,
            attachments=attachments,
            attachments_revision=attachments_revision,
            baseline=baseline,
            stable_major_floor=baseline.floor if baseline is not None else None,
            ref_facts=ref_facts,
        )
        # WR-04: end the read-only transaction so no pooled connection sits idle in transaction
        # across the remote call (up to the publish timeout). The locked section begins again.
        await session.rollback()
        verdict = await gate.check(bytes(row.raw_document), row.media_type, settings, ctx)
        parsed = verdict.parsed

    # The links of the gated document, read before any lock (CPU only, bounded by the cap).
    refs = get_adapter(contract_notation).references(parsed.document) if parsed is not None else []

    # (d) contract first, then the draft; lock_draft also refuses a row that is not a draft.
    contract = await lock_contract(session, contract_id)
    draft = await lock_draft(session, contract_id, draft_id)
    if parsed is None or verdict is None:  # pragma: no cover - a non-draft row never comes back
        raise _not_found()

    # (e) under the locks: the revision and the bytes must be the ones that were checked.
    require_if_match(if_match, etag_for_draft(draft.revision))
    if draft.content_sha256 != parsed.content_sha256:
        raise ProblemError(
            412,
            "precondition-failed",
            "Precondition Failed",
            "The resource has changed since you last read it; reload it and retry.",
        )
    # (e2) D-124, D-131: the attachments this publish was validated with must still be the
    # contract's; a change since the gate read them (attach, re-pin or detach) forces a retry.
    if verdict.attachments_revision != contract.attachments_revision:
        raise ProblemError(
            409,
            "rules-changed",
            "Conflict",
            "The attached rule sets changed while the contract was being validated; retry.",
        )
    # (f) the document id must still be the contract's.
    if parsed.odcs_id != contract.odcs_id:
        raise odcs_id_mismatch(contract.odcs_id, parsed.odcs_id)
    # (g) D-46: a quoted SemVer 2.0.0 string, unused and strictly above every non-draft row.
    parts = validate_publish(
        get_adapter(contract.notation).declared_version(parsed.document),
        await semver_rows(session, contract_id),
    )

    # (g1) D-147: after the SemVer checks (a taken or lower version is the more specific 409), the
    # baseline and the floor this publish was validated against must still be the
    # highest published version and the highest stable major; an unpublish, republish or another
    # publish since the gate read them forces a retry, with nothing written.
    current_baseline, current_floor = await baseline_key(session, contract_id)
    if current_baseline != verdict.baseline_id or current_floor != verdict.stable_major_floor:
        raise ProblemError(
            409,
            "baseline-changed",
            "Conflict",
            "The published versions changed while the contract was being validated; retry.",
        )
    # (g2) D-54: lock every contract the document may link to (FOR KEY SHARE, by id) and record
    # the links; the version is still the draft here and becomes published only at (h).
    await index_references(session, contract, draft, refs)
    # (g3) IN-03, D-153: the link facts validation judged must still hold. The targets are locked
    # by (g2); recheck_link_facts locks their version rows too and recomputes the facts from the
    # database with the scopes read before the gate (no identity call under a lock). A fact that got
    # worse since the gate (a linked version unpublished, a target deleted) forces a retry.
    if link_facts is not None and not await recheck_link_facts(session, link_facts):
        raise ProblemError(
            409,
            "baseline-changed",
            "Conflict",
            "A contract this document links to changed while the contract was being validated; "
            "retry.",
        )

    # (h) the draft becomes the version, in place.
    before = version_snapshot(draft)
    first = contract.latest_version_id is None
    draft.state = "published"
    draft.semver = parts.text
    draft.semver_major = parts.major
    draft.semver_minor = parts.minor
    draft.semver_patch = parts.patch
    draft.semver_prerelease = parts.prerelease
    draft.published_at = func.now()
    draft.published_by = uow.actor.id
    # D-132: what governance validated these bytes against, frozen with the publish (the guard
    # trigger refuses any later change). A test double's empty result records an empty list.
    draft.validation_run_id = verdict.run_id
    draft.pinned_rule_sets = verdict.pins
    draft.engine_version = verdict.engine_version
    draft.schema_sha = verdict.schema_sha
    if first:
        draft.is_latest = True
        contract.latest_version_id = draft.id
    await session.flush()
    await session.refresh(draft)
    await session.refresh(contract)

    # A publish is always above every existing version (D-46), so a non-first one is a Preview.
    status = derive_status(parts, None, is_latest=first)
    # (i) one metadata-only event.
    uow.audit(
        "contract.version_published",
        "contract",
        contract.id,
        before=before,
        after={
            **version_snapshot(draft),
            "status": status,
            "pinned_rule_sets": verdict.pins,
            # D-101: every card with an active pin on this contract, read under the locks above.
            "notify_card_ids": await audience_for_contract(session, contract.id),
            # D-163: which version each of them pins, so the consumer can diff each pinned version
            # against this one off the publish path (identifiers only; a redelivery plans the same).
            "notify_pins": await pins_for_contract(session, contract.id),
        },
        scope_path=contract.ancestor_ids,
        aggregate_version=draft.version_no,
    )
    return PublishResult(version=draft, status=status)


async def create_draft(
    uow: UnitOfWork,
    contract_id: uuid.UUID,
    *,
    from_version: str | None,
    from_draft_id: uuid.UUID | None,
    bump_kind: BumpKind,
    settings: ContractsSettings,
) -> ContractVersion:
    """Start a new draft of the contract from one of its versions or drafts (D-42, D-45).

    The copy keeps the source's bytes and media type; only the top-level ``version`` token moves
    (``OdcsAdapter.copy_with``), or nothing for ``bump none``. All CPU work (the in-place edit and
    the D-19 gate) runs before the contract lock, which is then held only for the cap check, the
    row number and the insert (one audit event ``contract.draft_created``).
    """
    session = uow.session
    notation = (
        await session.execute(select(Contract.notation).where(Contract.id == contract_id))
    ).scalar_one_or_none()
    if notation is None:
        raise _not_found()

    source: ContractVersion | None
    if from_draft_id is not None:
        source = await get_draft(session, contract_id, from_draft_id)
    elif from_version is not None and parse_semver(from_version) is not None:
        source = await resolve_ref(session, contract_id, from_version)
    else:
        source = None
    if source is None:
        raise _not_found()

    adapter = get_adapter(notation)
    raw = bytes(source.raw_document)
    media_type = source.media_type
    if bump_kind != "none":
        new_version = bump(adapter.declared_version(source.document), bump_kind)
        raw = await asyncio.to_thread(
            adapter.copy_with, raw, media_type, odcs_id=None, version=new_version
        )
    parsed = await parse_upload(raw, media_type, settings)
    fields = extract_search_fields(parsed.document)
    source_id = source.id

    contract = await lock_contract(session, contract_id)
    drafts = (
        await session.execute(
            select(func.count())
            .select_from(ContractVersion)
            .where(ContractVersion.contract_id == contract_id, ContractVersion.state == "draft")
        )
    ).scalar_one()
    if drafts >= settings.max_drafts_per_contract:
        raise ProblemError(
            409,
            "draft-limit-reached",
            "Conflict",
            "This contract already has the maximum number of drafts; publish or discard one first.",
        )
    version_no = contract.version_counter + 1
    contract.version_counter = version_no
    draft = ContractVersion(
        contract_id=contract_id,
        version_no=version_no,
        state="draft",
        revision=1,
        base_version_id=source_id,
        odcs_api_version=parsed.api_version,
        raw_document=raw,
        media_type=parsed.media_type,
        content_sha256=parsed.content_sha256,
        size_bytes=parsed.size_bytes,
        document=parsed.document,
        name=fields.name,
        domain=fields.domain,
        status=fields.status,
        tags=list(fields.tags),
        description_text=fields.description_text,
        odcs_version=fields.odcs_version,
    )
    session.add(draft)
    await session.flush()
    await session.refresh(draft)
    uow.audit(
        "contract.draft_created",
        "contract",
        contract.id,
        before=None,
        after=draft_snapshot(draft),
        scope_path=contract.ancestor_ids,
        aggregate_version=version_no,
    )
    return draft


async def delete_draft(
    uow: UnitOfWork, contract_id: uuid.UUID, draft_id: uuid.UUID, if_match: str | None
) -> None:
    """Discard a draft (D-42). Same lock order as every lifecycle write: contract, then draft.

    A never-published contract keeps its only row (409 ``last-draft-protected``); a published
    draft id is read-only (409 ``draft-already-published``, from ``lock_draft``).
    """
    session = uow.session
    contract = await lock_contract(session, contract_id)
    draft = await lock_draft(session, contract_id, draft_id)
    require_if_match(if_match, etag_for_draft(draft.revision))
    if contract.latest_version_id is None:
        rows = (
            await session.execute(
                select(func.count())
                .select_from(ContractVersion)
                .where(ContractVersion.contract_id == contract_id)
            )
        ).scalar_one()
        if rows <= 1:
            raise ProblemError(
                409,
                "last-draft-protected",
                "Conflict",
                "A contract that was never published keeps at least one draft; delete the "
                "contract instead.",
            )
    before = draft_snapshot(draft)
    version_no = draft.version_no
    await session.delete(draft)
    await session.flush()
    uow.audit(
        "contract.draft_deleted",
        "contract",
        contract.id,
        before=before,
        after=None,
        scope_path=contract.ancestor_ids,
        aggregate_version=version_no,
    )


async def lock_version(session: AsyncSession, contract_id: uuid.UUID, ref: str) -> ContractVersion:
    """The non-draft version a lifecycle write names, under ``FOR UPDATE``; 404 otherwise.

    Lifecycle writes address a version by strict SemVer only: the ``latest`` alias is read-only
    (it would resolve ambiguously under concurrency), and an unparsable or unknown reference is
    the same 404 as an unknown contract. ``populate_existing`` overwrites a stale identity-map copy
    with the locked row (research Pitfall 1). Call after ``lock_contract`` (lock order).
    """
    parts = parse_semver(ref)
    if ref == "latest" or parts is None:
        raise _not_found()
    version = (
        await session.execute(
            select(ContractVersion)
            .where(
                ContractVersion.contract_id == contract_id,
                ContractVersion.state != "draft",
                ContractVersion.semver_major == parts.major,
                ContractVersion.semver_minor == parts.minor,
                ContractVersion.semver_patch == parts.patch,
                ContractVersion.semver_prerelease == parts.prerelease,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if version is None:
        raise _not_found()
    return version


def _version_not_published() -> ProblemError:
    return ProblemError(
        409,
        "version-not-published",
        "Conflict",
        "Only a published version can be used for this operation.",
    )


async def promote_version(
    uow: UnitOfWork, contract_id: uuid.UUID, ref: str
) -> tuple[ContractVersion, bool]:
    """Make a published version the contract's Latest (VER-05, D-39, D-53).

    Returns ``(version, changed)``; promoting the current Latest is a no-op that touches nothing
    and records nothing. The unique index on ``is_latest`` is checked per row, so the old flag is
    cleared and flushed before the new flag and the pointer are set (research Pitfall 2). Promoting
    a deprecated version clears its deprecation (D-53). Never a single UPDATE that flips both flags.
    """
    session = uow.session
    contract = await lock_contract(session, contract_id)
    target = await lock_version(session, contract_id, ref)
    if target.state != "published":
        raise _version_not_published()
    if contract.latest_version_id == target.id:
        return target, False

    old: ContractVersion | None = None
    if contract.latest_version_id is not None:
        old = (
            await session.execute(
                select(ContractVersion)
                .where(
                    ContractVersion.id == contract.latest_version_id,
                    ContractVersion.contract_id == contract_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
    before = {
        "latest_version_id": None if old is None else str(old.id),
        "latest_semver": None if old is None else old.semver,
    }
    if old is not None:
        old.is_latest = False
        await session.flush()  # statement 1: clears the old flag
    target.is_latest = True
    target.deprecated_at = None
    target.deprecated_by = None
    target.sunset_date = None
    contract.latest_version_id = target.id
    await session.flush()  # statement 2: sets the new flag and the pointer
    await session.refresh(target)
    await session.refresh(contract)
    uow.audit(
        "contract.version_promoted",
        "contract",
        contract.id,
        before=before,
        after={
            "latest_version_id": str(target.id),
            "latest_semver": target.semver,
            "version": version_snapshot(target),
            # D-101: every card with an active pin on this contract, read under the locks above.
            "notify_card_ids": await audience_for_contract(session, contract.id),
        },
        scope_path=contract.ancestor_ids,
        aggregate_version=target.version_no,
    )
    return target, True


async def unpublish_version(
    uow: UnitOfWork, contract_id: uuid.UUID, ref: str, reason: str | None = None
) -> tuple[ContractVersion, bool]:
    """Hide a published version from consumers without deleting it (VER-06, D-39, D-52).

    The current Latest cannot be unpublished (409 ``latest-version-protected``; the database
    backstop is ``ck_contract_version_latest_is_published``). Unpublishing an unpublished version
    is a no-op. A version any card actively pins is refused too (409 ``version-pinned``, D-91,
    D-95): the refusal lists every reason in one ``LifecycleBlocked`` (D-106) and names every
    pinning card whatever its organization. The caller records the blocked attempt after rolling
    its unit back (``pinned_card_ids`` says whom to tell). A ``reason`` (quick 261120-0ye, G3) is
    recorded in the event's ``after``.
    """
    session = uow.session
    contract = await lock_contract(session, contract_id)
    version = await lock_version(session, contract_id, ref)
    if version.state == "unpublished":
        return version, False
    # Collect every blocker before raising, in the fixed order latest, pinned (D-106). The pin
    # query runs under both locks and before any write, so a subscribe either committed first
    # (and is seen here) or waits on the contract lock and then finds the version unpublished.
    reasons: list[dict[str, Any]] = []
    if contract.latest_version_id == version.id:
        reasons.append({"code": REASON_LATEST})
    pinned = await pinned_cards_for_version(session, version.id)
    if pinned:
        reasons.append(
            {"code": REASON_VERSION_PINNED, "cards": [pinned_card_out(c) for c in pinned]}
        )
    if reasons:
        raise LifecycleBlocked(
            reasons,
            detail=(
                "The current Latest cannot be unpublished; promote another version first."
                if reasons[0]["code"] == REASON_LATEST
                else "Cards are pinned to this version; remove their subscriptions first."
            ),
            version_id=version.id,
            semver=version.semver,
            pinned_card_ids=tuple(c.card_id for c in pinned),
        )
    before = version_snapshot(version)
    version.state = "unpublished"
    version.unpublished_at = func.now()
    version.unpublished_by = uow.actor.id
    await session.flush()
    await session.refresh(version)
    uow.audit(
        "contract.version_unpublished",
        "contract",
        contract.id,
        before=before,
        after={
            **version_snapshot(version),
            **({"reason": reason} if reason is not None else {}),
        },
        scope_path=contract.ancestor_ids,
        aggregate_version=version.version_no,
    )
    return version, True


async def republish_version(
    uow: UnitOfWork, contract_id: uuid.UUID, ref: str
) -> tuple[ContractVersion, bool]:
    """Restore an unpublished version: same bytes, same semver, never automatically Latest (D-52).

    Republishing a published version is a no-op. The Latest pointer is not touched, so the
    version returns as a Preview or an older version by SemVer. A version whose stored links
    resolve to a soft-deleted contract is refused with 409 ``contract-referenced`` (WR-03): the
    delete guard only saw published links, so republishing would re-attach a deleted target.
    """
    session = uow.session
    contract = await lock_contract(session, contract_id)
    version = await lock_version(session, contract_id, ref)
    if version.state == "published":
        return version, False
    # WR-03: while unpublished its links blocked nothing, so a target may have been deleted since.
    dangling = await deleted_link_targets(session, contract, [version.id])
    if dangling:
        raise _links_to_deleted(
            dangling,
            detail=(
                "This version links to a deleted contract; restore that contract or publish a "
                "new version without the link."
            ),
        )
    before = version_snapshot(version)
    version.state = "published"
    version.unpublished_at = None
    version.unpublished_by = None
    await session.flush()
    await session.refresh(version)
    uow.audit(
        "contract.version_republished",
        "contract",
        contract.id,
        before=before,
        after=version_snapshot(version),
        scope_path=contract.ancestor_ids,
        aggregate_version=version.version_no,
    )
    return version, True


async def deprecate_version(
    uow: UnitOfWork,
    contract_id: uuid.UUID,
    ref: str,
    sunset_date: date,
    reason: str | None = None,
) -> tuple[ContractVersion, bool]:
    """Flag a published version as deprecated with a sunset date (VER-07, D-49, A4).

    The sunset date must be today or later (UTC). The first deprecation stamps ``deprecated_at``;
    a later call with another date changes only ``sunset_date`` (the Deprecation header keeps its
    original moment); the same date again is a no-op. The version stays fetchable regardless.
    The current Latest may be deprecated (flag only). A ``reason`` (quick 261120-0ye, G3) is
    recorded in the event's ``after`` and reaches the deprecated notice from there.
    """
    if sunset_date < datetime.now(UTC).date():
        raise ProblemError(
            422,
            "invalid-sunset-date",
            "Unprocessable Content",
            "The sunset date must be today or later.",
        )
    session = uow.session
    contract = await lock_contract(session, contract_id)
    version = await lock_version(session, contract_id, ref)
    if version.state != "published":
        raise _version_not_published()
    if version.deprecated_at is not None and version.sunset_date == sunset_date:
        return version, False
    before = version_snapshot(version)
    if version.deprecated_at is None:
        version.deprecated_at = func.now()
        version.deprecated_by = uow.actor.id
    version.sunset_date = sunset_date
    await session.flush()
    await session.refresh(version)
    # D-99: the cards to notify travel in this operation's own event, read under the locks above
    # (one indexed read, no write); the notice work itself happens when the event is handled.
    notify = await audience_for_version(session, version.id)
    uow.audit(
        "contract.version_deprecated",
        "contract",
        contract.id,
        before=before,
        after={
            **version_snapshot(version),
            "notify_card_ids": notify,
            **({"reason": reason} if reason is not None else {}),
        },
        scope_path=contract.ancestor_ids,
        aggregate_version=version.version_no,
    )
    return version, True


__all__ = [
    "PassThroughPublishGate",
    "PublishGate",
    "PublishResult",
    "Referrer",
    "contract_snapshot",
    "create_draft",
    "delete_contract",
    "delete_draft",
    "deprecate_version",
    "get_publish_gate",
    "latest_parts",
    "list_versions",
    "lock_contract",
    "lock_version",
    "promote_version",
    "publish_draft",
    "read_contract",
    "republish_version",
    "resolve_ref",
    "restore_contract",
    "semver_rows",
    "unpublish_version",
    "version_snapshot",
    "visible_states",
]
