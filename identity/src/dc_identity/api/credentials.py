"""Personal access token routes: ``/api/v1/identity/pats`` (AUTH-06, D-66, D-67, D-68, D-71).

The ``/admin/pats`` routes are the Global admin's oversight view (metadata only, revoke any).
Every authenticated caller manages only their own PATs: ``pat:list`` and ``pat:manage`` are
principal-level permissions, and the handlers key every row on the acting user. The create
response is the only place the ``dcp_`` token is ever shown.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from dc_auth.deps import UnitOfWorkDep, authenticate, require
from dc_auth.principal import Principal
from dc_core.app import read_session
from dc_core.names import NameRule
from dc_core.policy import PolicyRouter
from fastapi import Depends, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from dc_identity.api.folders import _decode_cursor, _encode_cursor
from dc_identity.credentials import (
    count_live_pats,
    create_pat,
    list_all_pats,
    list_own_pats,
    revoke_any_pat,
    revoke_own_pat,
)
from dc_identity.models import ApiCredential
from dc_identity.security_policy import request_policy
from dc_identity.settings import IdentitySettings

router = PolicyRouter(prefix="/api/v1/identity")

PatName = Annotated[str, StringConstraints(min_length=1, max_length=100), NameRule]


class IdentityPatCreateIn(BaseModel):
    # extra="forbid": no server-owned field (revoked_at, owner) can be sent.
    model_config = ConfigDict(extra="forbid")

    name: PatName
    expires_at: AwareDatetime
    read_only: bool = False  # D-66: hold only the read permissions of the owner


class IdentityPatCreatedOut(BaseModel):
    id: uuid.UUID
    name: str
    read_only: bool
    expires_at: datetime
    created_at: datetime
    token: str


class IdentityPatOut(BaseModel):
    id: uuid.UUID
    name: str
    read_only: bool
    expires_at: datetime
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None
    status: str  # active, expired or revoked


class IdentityPatListOut(BaseModel):
    items: list[IdentityPatOut]
    next_cursor: str | None
    # The caller's live (unrevoked, unexpired) PATs and the effective limit, so a screen knows the
    # cap without loading every page (quick 261120-lbz, D-03).
    active_count: int
    max_active: int


class IdentityAdminPatOut(BaseModel):
    """One PAT as the Global admin sees it: metadata only, never a token, secret, id or hash."""

    id: uuid.UUID
    owner_principal_id: str
    name: str
    read_only: bool
    expires_at: datetime
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None
    status: str  # active, expired or revoked


class IdentityAdminPatListOut(BaseModel):
    items: list[IdentityAdminPatOut]
    next_cursor: str | None


def _out(row: ApiCredential, status: str) -> IdentityPatOut:
    return IdentityPatOut(
        id=row.id,
        name=row.name,
        read_only=row.read_only,
        expires_at=row.expires_at,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
        status=status,
    )


@router.post(
    "/pats",
    status_code=201,
    response_model=IdentityPatCreatedOut,
    dependencies=[require("pat:manage")],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "Only a signed-in human user may create a personal access token"},
        409: {"description": "The caller already holds the maximum number of active tokens"},
        422: {"description": "Validation error, or the expiry is outside the allowed range"},
    },
)
async def create_pat_route(
    body: IdentityPatCreateIn,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityPatCreatedOut:
    settings: IdentitySettings = request.app.state.settings
    policy = await request_policy(request)
    row, token = await create_pat(
        uow,
        principal,
        name=body.name,
        expires_at=body.expires_at,
        settings=settings,
        read_only=body.read_only,
        max_days=policy.credential_max_ttl_days,
        max_active=policy.max_active_tokens,
    )
    response.headers["Location"] = f"/api/v1/identity/pats/{row.id}"
    return IdentityPatCreatedOut(
        id=row.id,
        name=row.name,
        read_only=row.read_only,
        expires_at=row.expires_at,
        created_at=row.created_at,
        token=token,
    )


@router.get(
    "/pats",
    response_model=IdentityPatListOut,
    dependencies=[require("pat:list")],
    responses={
        401: {"description": "No valid token"},
        422: {"description": "Invalid cursor or limit"},
    },
)
async def list_pats(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> IdentityPatListOut:
    rows, next_id = await list_own_pats(
        session,
        principal,
        cursor=_decode_cursor(cursor) if cursor is not None else None,
        limit=limit,
    )
    return IdentityPatListOut(
        items=[_out(row, status) for row, status in rows],
        next_cursor=_encode_cursor(next_id) if next_id is not None else None,
        active_count=await count_live_pats(session, principal),
        max_active=(await request_policy(request)).max_active_tokens,
    )


@router.delete(
    "/pats/{credential_id}",
    status_code=204,
    dependencies=[require("pat:manage")],
    responses={
        204: {"description": "The token is revoked (also when it already was)"},
        401: {"description": "No valid token"},
        404: {"description": "No such token among the caller's own"},
    },
)
async def revoke_pat_route(
    credential_id: uuid.UUID,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> Response:
    await revoke_own_pat(uow, principal, credential_id)
    return Response(status_code=204)


@router.get(
    "/admin/pats",
    response_model=IdentityAdminPatListOut,
    dependencies=[require("pat:read-all")],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "Only a Global admin may list every personal access token"},
        422: {"description": "Invalid cursor, limit, owner or status"},
    },
)
async def list_all_pats_route(
    session: Annotated[AsyncSession, Depends(read_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
    owner: Annotated[str | None, Query(max_length=300)] = None,
    status: Annotated[Literal["active", "expired", "revoked"] | None, Query()] = None,
) -> IdentityAdminPatListOut:
    rows, next_id = await list_all_pats(
        session,
        owner=owner,
        status=status,
        cursor=_decode_cursor(cursor) if cursor is not None else None,
        limit=limit,
    )
    return IdentityAdminPatListOut(
        items=[
            IdentityAdminPatOut(
                id=row.id,
                owner_principal_id=owner_id,
                name=row.name,
                read_only=row.read_only,
                expires_at=row.expires_at,
                created_at=row.created_at,
                revoked_at=row.revoked_at,
                last_used_at=row.last_used_at,
                status=state,
            )
            for row, owner_id, state in rows
        ],
        next_cursor=_encode_cursor(next_id) if next_id is not None else None,
    )


@router.delete(
    "/admin/pats/{credential_id}",
    status_code=204,
    dependencies=[require("pat:revoke-any")],
    responses={
        204: {"description": "The token is revoked (also when it already was)"},
        401: {"description": "No valid token"},
        403: {"description": "Only a Global admin signed in interactively may revoke any token"},
        404: {"description": "No such token"},
    },
)
async def revoke_any_pat_route(
    credential_id: uuid.UUID,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> Response:
    await revoke_any_pat(uow, principal, credential_id)
    return Response(status_code=204)
