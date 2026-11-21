"""The contract reference index and the delete guard it feeds (CONT-04, D-54).

Publishing a version stores the links it contains (``dc_odcs.refs``) as text keys in
``contract_reference``; deleting a contract asks which other contracts' *published* versions link
to it. A link is resolved at delete time, never at publish time, so a target created after its
referrer was published is still protected.

Resolution rule (the same one at publish and at delete):

* a ``contract_uuid`` link names exactly that contract, in any Organization;
* an ``odcs_id`` link names the contract with that ODCS id in the referrer's own Organization when
  the Organization has one (ODCS ids are unique per Organization and stay taken after a delete,
  D-09, D-55), and otherwise every contract with that ODCS id in any Organization. An ambiguous
  link therefore blocks rather than leaving a dangling reference (fail-closed).

There is deliberately no Organization filter on who may block a delete: visibility limits what a
refusal names (``versions.delete_contract``), never whether it happens. Deleted referrers, drafts,
unpublished versions and self-references never block. Because they do not block, bringing them back
is guarded from the other side (WR-03): ``deleted_link_targets`` refuses a republish or a restore
whose links resolve to a deleted contract, so no write re-attaches a published link to one.

This module never imports ``versions`` (``versions`` imports it), so there is no import cycle.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from dc_odcs import ContractRef
from sqlalchemy import ColumnElement, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from dc_contracts.models import Contract, ContractReference, ContractVersion


@dataclass(frozen=True)
class Referrer:
    """A contract whose published version links to another one; what the delete guard reports."""

    contract_id: uuid.UUID
    odcs_id: str
    folder_id: uuid.UUID
    ancestor_ids: tuple[uuid.UUID, ...]


async def index_references(
    session: AsyncSession, contract: Contract, version: ContractVersion, refs: Sequence[ContractRef]
) -> None:
    """Lock every contract the links may resolve to, then record one row per link.

    The candidates (any Organization, deleted ones and the publishing contract itself excluded)
    are locked ``FOR KEY SHARE`` ordered by id: a concurrent delete holds ``FOR UPDATE`` on its
    target, so exactly one of the two orders is observed, and no publish ever takes ``FOR
    UPDATE`` on another contract, so mutually referencing publishes cannot deadlock. Every link is
    stored, resolved or not, as the text it was written as.
    """
    if not refs:
        return
    await _lock_link_targets(
        session,
        contract,
        {uuid.UUID(ref.value) for ref in refs if ref.kind == "contract_uuid"},
        {ref.value for ref in refs if ref.kind == "odcs_id"},
    )

    for ref in refs:
        session.add(
            ContractReference(
                source_version_id=version.id,
                source_contract_id=contract.id,
                organization_id=contract.organization_id,
                ref_kind=ref.kind,
                ref_value=ref.value,
                pointer=ref.pointer,
            )
        )
    await session.flush()


async def _link_target_conditions(
    session: AsyncSession, contract: Contract, uuids: set[uuid.UUID], odcs_ids: set[str]
) -> list[ColumnElement[bool]]:
    """The resolution rule as SQL conditions on ``Contract``, deleted rows included."""
    local: set[str] = set()
    if odcs_ids:
        local = set(
            (
                await session.execute(
                    select(Contract.odcs_id).where(
                        Contract.organization_id == contract.organization_id,
                        Contract.odcs_id.in_(sorted(odcs_ids)),
                    )
                )
            )
            .scalars()
            .all()
        )
    conditions: list[ColumnElement[bool]] = []
    if uuids:
        conditions.append(Contract.id.in_(sorted(uuids)))
    if local:
        conditions.append(
            and_(
                Contract.organization_id == contract.organization_id,
                Contract.odcs_id.in_(sorted(local)),
            )
        )
    ambiguous = odcs_ids - local
    if ambiguous:
        conditions.append(Contract.odcs_id.in_(sorted(ambiguous)))
    return conditions


async def _lock_link_targets(
    session: AsyncSession, contract: Contract, uuids: set[uuid.UUID], odcs_ids: set[str]
) -> list[ColumnElement[bool]]:
    """Lock every non-deleted contract the links may resolve to (``FOR KEY SHARE``, by id).

    Returns the resolution conditions so a caller can run a second query over the same targets.
    """
    conditions = await _link_target_conditions(session, contract, uuids, odcs_ids)
    if conditions:
        await session.execute(
            select(Contract.id)
            .where(Contract.deleted_at.is_(None), Contract.id != contract.id, or_(*conditions))
            .order_by(Contract.id)
            .with_for_update(read=True, key_share=True)
        )
    return conditions


async def deleted_link_targets(
    session: AsyncSession, contract: Contract, version_ids: Sequence[uuid.UUID]
) -> int:
    """How many soft-deleted contracts the stored links of ``version_ids`` resolve to (WR-03).

    The reverse of ``blocking_referrers``, for the writes that bring a referrer's links back into
    force without publishing anything new: republishing an unpublished version and restoring a
    deleted contract. The caller holds its contract lock; this first locks every non-deleted
    target ``FOR KEY SHARE`` (the same lock and order as ``index_references``), so a concurrent
    delete of a target either committed first, and is counted here, or waits and then sees the
    links as published and is refused by its own guard. Self-links never count. Only a count is
    returned: a deleted contract is hidden from everyone, so a refusal never names it.
    """
    if not version_ids:
        return 0
    rows = (
        await session.execute(
            select(ContractReference.ref_kind, ContractReference.ref_value).where(
                ContractReference.source_version_id.in_(sorted(version_ids))
            )
        )
    ).all()
    uuids: set[uuid.UUID] = set()
    odcs_ids: set[str] = set()
    for row in rows:
        if row.ref_kind == "contract_uuid":
            try:
                uuids.add(uuid.UUID(row.ref_value))
            except ValueError:  # pragma: no cover - only parsed UUIDs are ever stored
                continue
        elif row.ref_kind == "odcs_id":
            odcs_ids.add(row.ref_value)
    if not uuids and not odcs_ids:
        return 0
    conditions = await _lock_link_targets(session, contract, uuids, odcs_ids)
    if not conditions:
        return 0
    return int(
        (
            await session.execute(
                select(func.count(Contract.id)).where(
                    Contract.deleted_at.is_not(None),
                    Contract.id != contract.id,
                    or_(*conditions),
                )
            )
        ).scalar_one()
    )


async def blocking_referrers(session: AsyncSession, target: Contract) -> list[Referrer]:
    """The distinct other contracts whose published versions link to ``target``, by contract id.

    One query. The caller holds ``FOR UPDATE`` on ``target``, so a publish that links to it is
    either already visible here or waits behind the delete and sees the contract deleted.
    """
    source = aliased(Contract)
    namesake = aliased(Contract)
    local_namesake_exists = exists().where(
        namesake.organization_id == ContractReference.organization_id,
        namesake.odcs_id == ContractReference.ref_value,
    )
    rows = (
        await session.execute(
            select(source.id, source.odcs_id, source.folder_id, source.ancestor_ids)
            .select_from(ContractReference)
            .join(ContractVersion, ContractVersion.id == ContractReference.source_version_id)
            .join(source, source.id == ContractReference.source_contract_id)
            .where(
                ContractVersion.state == "published",
                source.deleted_at.is_(None),
                source.id != target.id,
                or_(
                    and_(
                        ContractReference.ref_kind == "contract_uuid",
                        ContractReference.ref_value == str(target.id),
                    ),
                    and_(
                        ContractReference.ref_kind == "odcs_id",
                        ContractReference.ref_value == target.odcs_id,
                        or_(
                            ContractReference.organization_id == target.organization_id,
                            ~local_namesake_exists,
                        ),
                    ),
                ),
            )
            .distinct()
        )
    ).all()
    return sorted(
        (
            Referrer(
                contract_id=row.id,
                odcs_id=row.odcs_id,
                folder_id=row.folder_id,
                ancestor_ids=tuple(row.ancestor_ids),
            )
            for row in rows
        ),
        key=lambda referrer: referrer.contract_id,
    )


__all__ = ["Referrer", "blocking_referrers", "deleted_link_targets", "index_references"]
