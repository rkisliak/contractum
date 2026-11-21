"""Internal folder usage and purge routes, called by identity when a folder is deleted.

Quick 261119-0v1. ``GET /internal/v1/contracts/folders/{folder_id}/usage`` answers counts only:
the **blockers** (``contracts`` not soft-deleted in any state, ``templates``, ``subscriber_cards``
not archived) and the **purges** (``deleted_contracts``, ``archived_subscriber_cards``).
``POST /internal/v1/contracts/folders/{folder_id}/purge`` permanently removes the purgeable rows
(see ``dc_contracts.folder_purge``) and refuses with 409 ``folder-not-empty`` while a blocker
is left.

Both routes need ``folder:delete`` at the folder, checked by identity's PDP with the **caller's own
bearer** that identity forwards (no confused deputy, T-5-40): only a Global admin passes, and a
read-only token never does. They are internal: never in OpenAPI and never routed by the gateway.
Usage is a read; purge writes one counts-only ``folder.purged`` audit event.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from dc_auth.deps import UnitOfWorkDep, current_decision, require
from dc_core.app import read_session
from dc_core.policy import PolicyRouter
from fastapi import Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from dc_contracts.folder_purge import folder_usage, purge_folder

internal_router = PolicyRouter(prefix="/internal/v1/contracts")


class ContractsFolderUsageOut(BaseModel):
    folder_id: uuid.UUID
    contracts: int
    templates: int
    subscriber_cards: int
    deleted_contracts: int
    archived_subscriber_cards: int


class ContractsFolderPurgeOut(BaseModel):
    folder_id: uuid.UUID
    deleted_contracts: int
    contract_versions: int
    archived_subscriber_cards: int
    card_subscriptions: int
    origin_cleared: int


async def _path_folder_id(folder_id: uuid.UUID) -> uuid.UUID:
    return folder_id


@internal_router.get(
    "/folders/{folder_id}/usage",
    response_model=ContractsFolderUsageOut,
    dependencies=[require("folder:delete", target=_path_folder_id)],
)
async def contracts_folder_usage(
    folder_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> ContractsFolderUsageOut:
    return ContractsFolderUsageOut(folder_id=folder_id, **await folder_usage(session, folder_id))


@internal_router.post(
    "/folders/{folder_id}/purge",
    response_model=ContractsFolderPurgeOut,
    dependencies=[require("folder:delete", target=_path_folder_id)],
)
async def contracts_folder_purge(
    folder_id: uuid.UUID, request: Request, uow: UnitOfWorkDep
) -> ContractsFolderPurgeOut:
    facts = current_decision(request).folder
    scope = facts.ancestor_ids if facts is not None else (folder_id,)
    counts = await purge_folder(uow, folder_id, scope)
    return ContractsFolderPurgeOut(folder_id=folder_id, **counts)
