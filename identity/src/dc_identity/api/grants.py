"""Grant routes: ``/api/v1/identity`` folder grants and grant revocation (RBAC-03, RBAC-05).

Creation and listing are scoped by folder (``/folders/{folder_id}/grants``); reading and revoking
one grant is addressed by its id (``/grants/{grant_id}``). The ``require()`` gate checks
``grant:manage`` (or ``grant:read``) at the folder the grant sits on, so a caller who cannot see
that folder gets 404 and a reader who may not manage gets 403. The escalation guards (level
binding, ceiling, self-grant, last Global admin) run inside the write transaction in
:mod:`dc_identity.grants`; the gate alone is never trusted.

Grant principal wire shape: ``principal_type`` ``user`` carries the user's platform principal id,
``local:<uuid>`` (exactly ``IdentityUserOut.principal_id``) or ``entra:<tid>:<oid>`` of an Entra
user record. The id is shape-checked per type against ``PRINCIPAL_ID_PATTERNS`` (422
``validation-error``); a well-formed id that names no active user is 422 ``unknown-principal``.
A local user can instead be named by exact ``username`` (with ``principal_type`` ``user`` and no
``principal_id``), as a group member is: an Org admin may not list users, but can grant to one
whose username they know. An unknown or inactive username is 422 ``unknown-principal``.
``principal_type`` ``local_group`` carries the group's bare lowercase uuid
(``IdentityGroupOut.id``); an id naming no group visible to the caller is 404. ``entra_group``
carries the GUID of an active group registration: the directory confirms it in the registration's
tenant before anything is written (unknown 422, outage 503), and an unregistered GUID is 404 with
no directory call (D-64). ``service_principal`` carries ``entra:<tid>:<oid>`` of an active
service-principal registration (404 otherwise). ``service_account`` carries ``sa:<uuid>`` of an
active local service account (422 ``unknown-principal`` otherwise; an Organization-owned account
outside its Organization is 422 ``principal-scope-mismatch`` for a caller who can read it, else
404).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from dc_auth.deps import UnitOfWorkDep, authenticate, require
from dc_auth.principal import Principal
from dc_auth.roles import Role
from dc_core.app import read_session
from dc_core.policy import PolicyRouter
from fastapi import Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_identity.api.folders import _decode_cursor, _encode_cursor, _path_folder_id
from dc_identity.grants import (
    _not_found,
    confirm_entra_group,
    create_grant,
    grant_folder,
    local_user_principal,
    revoke_grant,
)
from dc_identity.models import RoleAssignment
from dc_identity.principal_ids import GUID_PATTERN
from dc_identity.text import NoNulStr

router = PolicyRouter(prefix="/api/v1/identity")

# Canonical shape of ``principal_id`` for each accepted ``principal_type``. Further types add an
# entry here when their routes arrive. A local group and an Entra group are a bare lowercase uuid
# (``IdentityGroupOut.id``, the group's object id); a user stays ``<namespace>:<id>``; a service
# principal is ``entra:<tenant guid>:<object guid>``.
_GUID = GUID_PATTERN.pattern
PRINCIPAL_ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "user": re.compile(r"^[a-z][a-z0-9_-]*:[A-Za-z0-9._:-]+$"),
    "local_group": re.compile(rf"^{_GUID}$"),
    "entra_group": re.compile(rf"^{_GUID}$"),
    "service_principal": re.compile(rf"^entra:{_GUID}:{_GUID}$"),
    "service_account": re.compile(rf"^sa:{_GUID}$"),
}


class IdentityGrantCreateIn(BaseModel):
    # extra="forbid": created_by, folder_id and every other server-owned field are a 422.
    model_config = ConfigDict(extra="forbid")

    principal_type: Literal[
        "user", "local_group", "entra_group", "service_principal", "service_account"
    ]
    principal_id: str | None = Field(default=None, min_length=1, max_length=200)
    # A local user by exact username instead of principal_id (principal_type "user" only).
    username: NoNulStr | None = Field(default=None, min_length=1, max_length=200)
    role: Role

    @model_validator(mode="after")
    def _principal_id_shape(self) -> Self:
        if (self.username is None) == (self.principal_id is None):
            raise ValueError("give exactly one of principal_id and username")
        if self.username is not None:
            if self.principal_type != "user":
                raise ValueError("username names a local user: principal_type must be user")
            return self
        assert self.principal_id is not None  # noqa: S101 - narrowed above
        if PRINCIPAL_ID_PATTERNS[self.principal_type].fullmatch(self.principal_id) is None:
            raise ValueError("principal_id does not have the shape of its principal_type")
        return self


class IdentityGrantOut(BaseModel):
    id: uuid.UUID
    folder_id: uuid.UUID
    principal_type: str
    principal_id: str
    role: str
    created_by: str
    created_at: datetime


class IdentityGrantListOut(BaseModel):
    items: list[IdentityGrantOut]
    next_cursor: str | None


def _out(grant: RoleAssignment) -> IdentityGrantOut:
    return IdentityGrantOut(
        id=grant.id,
        folder_id=grant.folder_id,
        principal_type=grant.principal_type,
        principal_id=grant.principal_id,
        role=grant.role,
        created_by=grant.created_by,
        created_at=grant.created_at,
    )


async def _grant_target(
    grant_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> uuid.UUID:
    return await grant_folder(grant_id, session)


@router.post(
    "/folders/{folder_id}/grants",
    status_code=201,
    response_model=IdentityGrantOut,
    dependencies=[require("grant:manage", target=_path_folder_id)],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "Grant ceiling exceeded, or granting to oneself"},
        404: {
            "description": (
                "Folder not found or not visible to the caller, or the group or service "
                "principal is not registered or not visible to the caller"
            )
        },
        409: {"description": "The grant already exists, or the folder is archived"},
        422: {
            "description": (
                "Validation error, role not bound to this folder kind, unknown principal, or "
                "an owned group, service principal or service account outside its Organization"
            )
        },
        503: {"description": "The directory is unavailable"},
    },
)
async def create_grant_route(
    folder_id: uuid.UUID,
    body: IdentityGrantCreateIn,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityGrantOut:
    principal_id = body.principal_id
    if body.username is not None:
        principal_id = await local_user_principal(uow.session, body.username)
    assert principal_id is not None  # noqa: S101 - the body validator requires one of the two
    if body.principal_type == "entra_group":
        await confirm_entra_group(
            request.app.state.session_factory, request.app.state.directory, principal_id
        )
    grant = await create_grant(
        uow,
        principal,
        folder_id,
        principal_type=body.principal_type,
        principal_id=principal_id,
        role=body.role.value,
    )
    response.headers["Location"] = f"/api/v1/identity/grants/{grant.id}"
    return _out(grant)


@router.get(
    "/folders/{folder_id}/grants",
    response_model=IdentityGrantListOut,
    dependencies=[require("grant:read", target=_path_folder_id)],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "The caller can read the folder but may not read its grants"},
        404: {"description": "Folder not found, or not visible to the caller"},
        422: {"description": "Invalid cursor"},
    },
)
async def list_grants(
    folder_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(read_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> IdentityGrantListOut:
    stmt = select(RoleAssignment).where(RoleAssignment.folder_id == folder_id)
    if cursor is not None:
        stmt = stmt.where(RoleAssignment.id > _decode_cursor(cursor))
    stmt = stmt.order_by(RoleAssignment.id).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars())
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1].id) if len(rows) > limit else None
    return IdentityGrantListOut(items=[_out(grant) for grant in page], next_cursor=next_cursor)


@router.get(
    "/grants/{grant_id}",
    response_model=IdentityGrantOut,
    dependencies=[require("grant:read", target=_grant_target)],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "The caller can read the folder but may not read its grants"},
        404: {"description": "Grant not found, or not visible to the caller"},
    },
)
async def get_grant(
    grant_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> IdentityGrantOut:
    grant = (
        await session.execute(select(RoleAssignment).where(RoleAssignment.id == grant_id))
    ).scalar_one_or_none()
    if grant is None:
        raise _not_found()
    return _out(grant)


@router.delete(
    "/grants/{grant_id}",
    status_code=204,
    dependencies=[require("grant:manage", target=_grant_target)],
    responses={
        204: {"description": "The grant was revoked"},
        401: {"description": "No valid token"},
        403: {"description": "Grant ceiling exceeded"},
        404: {"description": "Grant not found, or not visible to the caller"},
        409: {"description": "This is the last Global admin"},
    },
)
async def revoke_grant_route(
    grant_id: uuid.UUID,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> Response:
    await revoke_grant(uow, principal, grant_id)
    return Response(status_code=204)


__all__ = [
    "PRINCIPAL_ID_PATTERNS",
    "IdentityGrantCreateIn",
    "IdentityGrantListOut",
    "IdentityGrantOut",
    "router",
]
