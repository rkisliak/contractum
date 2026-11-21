"""Explicit rule-set attachments: a contract owner raises the bar above governance (RULE-06).

An attachment is one published rule set, held at the contract's folder, an ancestor, ROOT or the
platform, that applies to one contract on top of the mandatory sets (D-122, D-123). The attachment
rows belong to contracts; the rule sets belong to validation. So **visibility is decided by
validation**, with the caller's own bearer, before any lock is taken (no row lock across a network
call, D-86): ``ValidationClient.available_rule_set`` answers the published rule set as it is
visible at the contract's folder, or nothing. An unknown, unpublished, sibling-branch or
deprecated choice is one uniform 422 ``rule-set-not-attachable``; an outage is 503.

Every attach, re-pin and detach takes the contract lock (the same lock every lifecycle write
uses), bumps ``contract.attachments_revision`` and records one metadata-only audit event
(``contract.ruleset_attached`` / ``contract.ruleset_detached``). A publish sends the attachments
and the revision it read before its gate, and compares the revision under the lock, so attachments
that changed while the contract was being validated force a retry (D-124, D-131, ``versions``).

A rule set that already applies to the folder as mandatory is never an attachment row: attaching
it is a no-op that says so (D-122), and nothing a contract owner does can detach or pin away a
mandatory set (resolution keeps it mandatory).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.locks import lock_contract
from dc_contracts.models import Contract, ContractRuleSetAttachment
from dc_contracts.validation_client import ValidationClient

AttachStatus = Literal["created", "updated", "unchanged", "mandatory"]

NAME_MAX_CHARS = 200


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


def not_attachable() -> ProblemError:
    """422 for a rule set that is not published, visible and current at the contract's folder.

    One body for every reason (unknown, another branch's, never published, deprecated), and the
    caller's input is never echoed.
    """
    return ProblemError(
        422,
        "rule-set-not-attachable",
        "Unprocessable Content",
        "The rule set is not a published rule set visible at this contract's folder, "
        "or the requested version is deprecated or unknown.",
    )


def version_spec_text(version: Literal["latest"] | int) -> str:
    return version if isinstance(version, str) else str(version)


def attachment_snapshot(row: ContractRuleSetAttachment, revision: int) -> dict[str, Any]:
    """Allow-listed audit view of an attachment plus the revision it produced."""
    return {
        **UnitOfWork.snapshot(row),
        "attachment_id": str(row.id),
        "attachments_revision": revision,
    }


async def list_attachments(
    session: AsyncSession, contract_id: uuid.UUID
) -> tuple[list[ContractRuleSetAttachment], int]:
    """The contract's attachments (by lower-cased name, then id) and its attachments revision."""
    revision = (
        await session.execute(
            select(Contract.attachments_revision).where(
                Contract.id == contract_id, Contract.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if revision is None:
        raise _not_found()
    rows = (
        (
            await session.execute(
                select(ContractRuleSetAttachment)
                .where(ContractRuleSetAttachment.contract_id == contract_id)
                .order_by(
                    func.lower(ContractRuleSetAttachment.rule_set_name),
                    ContractRuleSetAttachment.rule_set_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows), revision


async def attachments_for_gate(
    session: AsyncSession, contract_id: uuid.UUID
) -> tuple[tuple[dict[str, Any], ...], int]:
    """What a publish sends to validation: ``{rule_set_id, version}`` per row, and the revision.

    Read without a lock before the gate; ``versions.publish_draft`` compares the revision again
    under the contract lock. ``version`` is ``"latest"`` or the exact number.
    """
    rows, revision = await list_attachments(session, contract_id)
    wire = tuple(
        {
            "rule_set_id": str(row.rule_set_id),
            "version": row.version_spec if row.version_spec == "latest" else int(row.version_spec),
        }
        for row in rows
    )
    return wire, revision


def _check_choice(body: dict[str, Any], version: Literal["latest"] | int) -> None:
    """422 unless the chosen version exists, is published and is not deprecated."""
    if version == "latest":
        if body.get("latest_active_version") is None:
            raise not_attachable()
        return
    for entry in body.get("versions") or ():
        if isinstance(entry, dict) and entry.get("version_no") == version:
            if entry.get("deprecated_at") is not None:
                raise not_attachable()
            return
    raise not_attachable()


async def attach(
    uow: UnitOfWork,
    client: ValidationClient,
    bearer: str,
    contract_id: uuid.UUID,
    rule_set_id: uuid.UUID,
    version: Literal["latest"] | int,
    *,
    max_attachments: int = 50,
) -> tuple[ContractRuleSetAttachment | None, AttachStatus]:
    """Attach (or re-pin) a published rule set; ``(row, status)``.

    Order: the contract's folder (no lock), validation's visibility answer (no lock), then the
    contract lock and the write. A rule set that applies as mandatory writes nothing.
    """
    session = uow.session
    folder_id = (
        await session.execute(
            select(Contract.folder_id).where(
                Contract.id == contract_id, Contract.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if folder_id is None:
        raise _not_found()
    # WR-04: end the read-only transaction so no pooled connection sits idle in transaction
    # across validation's visibility call; the contract lock below begins a new one.
    await session.rollback()
    body = await client.available_rule_set(bearer, folder_id, rule_set_id)
    if body is None:
        raise not_attachable()
    resolved = body.get("resolved")
    if body.get("mandatory") is True and isinstance(resolved, list) and resolved:
        return None, "mandatory"
    _check_choice(body, version)
    name = str(body.get("name", ""))[:NAME_MAX_CHARS]
    spec = version_spec_text(version)

    contract = await lock_contract(session, contract_id)
    existing = (
        await session.execute(
            select(ContractRuleSetAttachment)
            .where(
                ContractRuleSetAttachment.contract_id == contract_id,
                ContractRuleSetAttachment.rule_set_id == rule_set_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if existing is not None and existing.version_spec == spec:
        return existing, "unchanged"

    if existing is None:
        # WR-06: validation refuses a run with more attachments than its limit, which would make
        # the contract unpublishable; refuse the attach that crosses the limit instead.
        count = (
            await session.execute(
                select(func.count())
                .select_from(ContractRuleSetAttachment)
                .where(ContractRuleSetAttachment.contract_id == contract_id)
            )
        ).scalar_one()
        if count >= max_attachments:
            raise ProblemError(
                422,
                "validation-error",
                "Validation error",
                "A contract can have at most " + str(max_attachments) + " attached rule sets.",
                errors=[{"pointer": "/rule_set_id", "detail": "Too many attached rule sets."}],
            )

    before = attachment_snapshot(existing, contract.attachments_revision) if existing else None
    contract.attachments_revision += 1
    if existing is None:
        row = ContractRuleSetAttachment(
            contract_id=contract_id, rule_set_id=rule_set_id, rule_set_name=name, version_spec=spec
        )
        session.add(row)
        status: AttachStatus = "created"
    else:
        row = existing
        row.version_spec = spec
        row.rule_set_name = name
        status = "updated"
    await session.flush()
    await session.refresh(row)
    uow.audit(
        "contract.ruleset_attached",
        "contract",
        contract.id,
        before=before,
        after=attachment_snapshot(row, contract.attachments_revision),
        scope_path=contract.ancestor_ids,
    )
    return row, status


async def detach(uow: UnitOfWork, contract_id: uuid.UUID, rule_set_id: uuid.UUID) -> int:
    """Remove the attachment; returns the new attachments revision (404 when not attached)."""
    session = uow.session
    contract = await lock_contract(session, contract_id)
    row = (
        await session.execute(
            select(ContractRuleSetAttachment)
            .where(
                ContractRuleSetAttachment.contract_id == contract_id,
                ContractRuleSetAttachment.rule_set_id == rule_set_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found()
    before = attachment_snapshot(row, contract.attachments_revision)
    contract.attachments_revision += 1
    await session.delete(row)
    await session.flush()
    uow.audit(
        "contract.ruleset_detached",
        "contract",
        contract.id,
        before=before,
        after={
            "rule_set_id": str(rule_set_id),
            "attachments_revision": contract.attachments_revision,
        },
        scope_path=contract.ancestor_ids,
    )
    return contract.attachments_revision
