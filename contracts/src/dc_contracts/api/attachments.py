"""Rule-set attachment routes: ``/api/v1/contracts/contracts/{contract_id}/rule-sets`` (RULE-06).

A contract owner (``contract:update``) attaches published rule sets visible at the contract's
folder, and detaches them again; a Policy Manager or anyone with ``contract:read-metadata`` lists
them. The domain rules (visibility decided by validation with the caller's own bearer, the contract
lock, the revision counter, one audited event) live in ``dc_contracts.attachments``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from dc_auth.deps import UnitOfWorkDep, current_decision, parse_bearer, require
from dc_core.app import read_session
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from fastapi import Body, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.api.contracts import contract_folder, require_writable_folder
from dc_contracts.attachments import (
    AttachStatus,
    attach,
    detach,
    list_attachments,
)
from dc_contracts.models import ContractRuleSetAttachment
from dc_contracts.validation_client import ValidationClient

router = PolicyRouter(prefix="/api/v1/contracts")

MAX_VERSION_NO = 2_147_483_647
VersionSpec = Literal["latest"] | Annotated[int, Field(strict=True, ge=1, le=MAX_VERSION_NO)]


class ContractRuleSetAttachIn(BaseModel):
    """Which version of the rule set to attach: ``latest`` (the default) or an exact number."""

    version: VersionSpec = "latest"


class ContractRuleSetAttachmentOut(BaseModel):
    """One attachment. ``rule_set_name`` is a snapshot taken when the rule set was attached."""

    rule_set_id: uuid.UUID
    rule_set_name: str
    version: VersionSpec
    attached_by: str
    attached_at: datetime


class ContractRuleSetAttachmentListOut(BaseModel):
    """The contract's attachments by name, and the revision a publish validates against."""

    items: list[ContractRuleSetAttachmentOut]
    attachments_revision: int


class ContractRuleSetAttachResultOut(BaseModel):
    """What an attach did: ``created``, ``updated``, ``unchanged`` or ``mandatory`` (no row)."""

    attachment: ContractRuleSetAttachmentOut | None
    status: Literal["created", "updated", "unchanged", "mandatory"]
    attachments_revision: int


def _attachment_out(row: ContractRuleSetAttachment) -> ContractRuleSetAttachmentOut:
    spec = row.version_spec
    return ContractRuleSetAttachmentOut(
        rule_set_id=row.rule_set_id,
        rule_set_name=row.rule_set_name,
        version="latest" if spec == "latest" else int(spec),
        attached_by=row.updated_by,
        attached_at=row.updated_at,
    )


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


_COMMON_RESPONSES: dict[int | str, dict[str, object]] = {
    404: {"description": "Contract not found, or not visible to the caller"},
    503: {"description": "Authorization or contract validation is temporarily unavailable"},
}


@router.get(
    "/contracts/{contract_id}/rule-sets",
    response_model=ContractRuleSetAttachmentListOut,
    dependencies=[require("contract:read-metadata", target=contract_folder)],
    responses=_COMMON_RESPONSES,
)
async def list_contract_rule_sets(
    contract_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> ContractRuleSetAttachmentListOut:
    """The rule sets attached to the contract, ordered by name, with the attachments revision."""
    rows, revision = await list_attachments(session, contract_id)
    return ContractRuleSetAttachmentListOut(
        items=[_attachment_out(row) for row in rows], attachments_revision=revision
    )


@router.put(
    "/contracts/{contract_id}/rule-sets/{rule_set_id}",
    status_code=201,
    response_model=ContractRuleSetAttachResultOut,
    dependencies=[require("contract:update", target=contract_folder)],
    responses={
        **_COMMON_RESPONSES,
        200: {
            "description": "Already attached with this version (unchanged), re-pinned (updated) "
            "or already applying as mandatory (status mandatory, nothing written)"
        },
        403: {"description": "The caller can read the contract but may not update it"},
        409: {"description": "The folder is archived"},
        422: {
            "description": "The rule set is not a published rule set visible at the contract's "
            "folder, or the requested version is deprecated or unknown (rule-set-not-attachable)"
        },
    },
)
async def attach_contract_rule_set(
    contract_id: uuid.UUID,
    rule_set_id: uuid.UUID,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
    session: Annotated[AsyncSession, Depends(read_session)],
    body: Annotated[ContractRuleSetAttachIn | None, Body()] = None,
) -> ContractRuleSetAttachResultOut:
    """Attach a published rule set (latest by default) or re-pin it to another version."""
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    require_writable_folder(facts)
    client: ValidationClient = request.app.state.validation_client
    chosen = (body or ContractRuleSetAttachIn()).version
    row, status = await attach(
        uow,
        client,
        parse_bearer(request),
        contract_id,
        rule_set_id,
        chosen,
        max_attachments=request.app.state.settings.max_attachments,
    )
    await uow.commit()
    _, revision = await list_attachments(session, contract_id)
    return _result(request, response, row, status, revision)


def _result(
    request: Request,
    response: Response,
    row: ContractRuleSetAttachment | None,
    status: AttachStatus,
    revision: int,
) -> ContractRuleSetAttachResultOut:
    if status == "created" and row is not None:
        response.status_code = 201
        response.headers["Location"] = str(request.url.path)
    else:
        response.status_code = 200
    return ContractRuleSetAttachResultOut(
        attachment=_attachment_out(row) if row is not None else None,
        status=status,
        attachments_revision=revision,
    )


@router.delete(
    "/contracts/{contract_id}/rule-sets/{rule_set_id}",
    status_code=204,
    dependencies=[require("contract:update", target=contract_folder)],
    responses={
        **_COMMON_RESPONSES,
        403: {"description": "The caller can read the contract but may not update it"},
        409: {"description": "The folder is archived"},
    },
)
async def detach_contract_rule_set(
    contract_id: uuid.UUID,
    rule_set_id: uuid.UUID,
    request: Request,
    uow: UnitOfWorkDep,
) -> Response:
    """Detach a rule set; 404 when it is not attached. Mandatory sets are never attachments."""
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    require_writable_folder(facts)
    await detach(uow, contract_id, rule_set_id)
    await uow.commit()
    return Response(status_code=204)
