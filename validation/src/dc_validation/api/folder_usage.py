"""Internal folder usage and purge routes, called by identity when a folder is deleted.

Quick 261119-0v1. ``GET /internal/v1/validation/folders/{folder_id}/usage`` answers counts only:
``rule_sets`` (in use: mandatory, they block the delete) and ``unused_rule_sets`` (removed with the
folder). ``POST /internal/v1/validation/folders/{folder_id}/purge`` permanently removes the unused
sets and their versions (see ``dc_validation.folder_purge``) and answers 409 ``folder-not-empty``
while a mandatory set is left.

Both need ``folder:delete`` at the folder, checked by identity's PDP with the caller's own bearer
forwarded by identity (no confused deputy, T-5-40). Internal: never in OpenAPI, never routed by the
gateway. The web process imports no rule engine here.
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

from dc_validation.folder_purge import folder_usage, purge_folder

internal_router = PolicyRouter(prefix="/internal/v1/validation")


class ValidationFolderUsageOut(BaseModel):
    folder_id: uuid.UUID
    rule_sets: int
    unused_rule_sets: int


class ValidationFolderPurgeOut(BaseModel):
    folder_id: uuid.UUID
    rule_sets: int
    rule_set_versions: int


async def _path_folder_id(folder_id: uuid.UUID) -> uuid.UUID:
    return folder_id


@internal_router.get(
    "/folders/{folder_id}/usage",
    response_model=ValidationFolderUsageOut,
    dependencies=[require("folder:delete", target=_path_folder_id)],
)
async def validation_folder_usage(
    folder_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> ValidationFolderUsageOut:
    return ValidationFolderUsageOut(folder_id=folder_id, **await folder_usage(session, folder_id))


@internal_router.post(
    "/folders/{folder_id}/purge",
    response_model=ValidationFolderPurgeOut,
    dependencies=[require("folder:delete", target=_path_folder_id)],
)
async def validation_folder_purge(
    folder_id: uuid.UUID, request: Request, uow: UnitOfWorkDep
) -> ValidationFolderPurgeOut:
    facts = current_decision(request).folder
    scope = facts.ancestor_ids if facts is not None else (folder_id,)
    counts = await purge_folder(uow, folder_id, scope)
    return ValidationFolderPurgeOut(folder_id=folder_id, **counts)
