"""Service account routes: ``/api/v1/identity/service-accounts`` (AUTH-07, D-69, D-70, D-78).

A service account is owned by ROOT or an Organization; ``service-account:create``, ``:read``,
``:manage`` and ``:manage-keys`` are evaluated at the owner folder, so an Organization admin reaches
exactly their Organization's accounts and a caller who cannot read the owner gets 404 (identical to
an unknown id). Creating and renaming are interactive-only (D-79).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from dc_auth.deps import UnitOfWorkDep, authenticate, require
from dc_auth.principal import Principal
from dc_core.app import read_session
from dc_core.names import NameRule
from dc_core.policy import PolicyRouter
from fastapi import Depends, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_identity.api.folders import _decode_cursor, _encode_cursor
from dc_identity.credentials import (
    create_service_account_key,
    list_service_account_keys,
    revoke_service_account_key,
)
from dc_identity.deactivation import deactivate_service_account, reactivate_service_account
from dc_identity.grants import _not_found
from dc_identity.models import ApiCredential, ServiceAccount
from dc_identity.principal_ids import service_account_principal_id
from dc_identity.security_policy import request_policy
from dc_identity.service_accounts import (
    create_service_account,
    list_service_accounts,
    rename_service_account,
    service_account_owner,
)
from dc_identity.settings import IdentitySettings

router = PolicyRouter(prefix="/api/v1/identity")

ServiceAccountName = Annotated[str, StringConstraints(min_length=1, max_length=100), NameRule]


class IdentityServiceAccountCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_folder_id: uuid.UUID
    name: ServiceAccountName


class IdentityServiceAccountPatchIn(BaseModel):
    # Required and non-nullable: JSON null, a missing name and extra fields are all a 422.
    model_config = ConfigDict(extra="forbid")

    name: ServiceAccountName


class IdentityServiceAccountOut(BaseModel):
    id: uuid.UUID
    principal_id: str
    owner_folder_id: uuid.UUID
    name: str
    is_active: bool
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime


class IdentityServiceAccountListOut(BaseModel):
    items: list[IdentityServiceAccountOut]
    next_cursor: str | None


class IdentityServiceAccountKeyCreateIn(BaseModel):
    # extra="forbid": no server-owned field (revoked_at, owner) can be sent; expiry is mandatory.
    model_config = ConfigDict(extra="forbid")

    name: ServiceAccountName
    expires_at: AwareDatetime


class IdentityServiceAccountKeyCreatedOut(BaseModel):
    id: uuid.UUID
    name: str
    expires_at: datetime
    created_at: datetime
    token: str


class IdentityServiceAccountKeyOut(BaseModel):
    """One key as an owner sees it: metadata only, never the token, secret, public id or hash."""

    id: uuid.UUID
    name: str
    expires_at: datetime
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None
    status: str  # active, expired or revoked
    expiring_soon: bool


class IdentityServiceAccountKeyListOut(BaseModel):
    items: list[IdentityServiceAccountKeyOut]


def _key_out(row: ApiCredential, status: str, expiring_soon: bool) -> IdentityServiceAccountKeyOut:
    return IdentityServiceAccountKeyOut(
        id=row.id,
        name=row.name,
        expires_at=row.expires_at,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
        status=status,
        expiring_soon=expiring_soon,
    )


def _out(account: ServiceAccount) -> IdentityServiceAccountOut:
    return IdentityServiceAccountOut(
        id=account.id,
        principal_id=service_account_principal_id(account.id),
        owner_folder_id=account.owner_folder_id,
        name=account.name,
        is_active=account.is_active,
        created_by=account.created_by,
        created_at=account.created_at,
        updated_by=account.updated_by,
        updated_at=account.updated_at,
    )


async def _create_owner_id(body: IdentityServiceAccountCreateIn) -> uuid.UUID:
    return body.owner_folder_id


async def _service_account_target(
    service_account_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> uuid.UUID:
    return await service_account_owner(service_account_id, session)


@router.post(
    "/service-accounts",
    status_code=201,
    response_model=IdentityServiceAccountOut,
    dependencies=[require("service-account:create", target=_create_owner_id)],
    responses={
        401: {"description": "No valid token"},
        403: {
            "description": "The caller can read the owner folder but may not create, "
            "or the caller did not sign in interactively"
        },
        404: {"description": "Owner folder not found, or not visible to the caller"},
        409: {"description": "The name is taken under this owner, or the owner is archived"},
        422: {"description": "Validation error, or the owner is not ROOT or an Organization"},
    },
)
async def create_service_account_route(
    body: IdentityServiceAccountCreateIn,
    response: Response,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityServiceAccountOut:
    account = await create_service_account(
        uow, principal, owner_folder_id=body.owner_folder_id, name=body.name
    )
    response.headers["Location"] = f"/api/v1/identity/service-accounts/{account.id}"
    return _out(account)


@router.get(
    "/service-accounts",
    response_model=IdentityServiceAccountListOut,
    dependencies=[require("service-account:list")],
    responses={
        401: {"description": "No valid token"},
        422: {"description": "Invalid cursor"},
    },
)
async def list_service_accounts_route(
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
    owner_folder_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> IdentityServiceAccountListOut:
    rows = await list_service_accounts(
        session,
        principal,
        owner_folder_id=owner_folder_id,
        limit=limit,
        after=_decode_cursor(cursor) if cursor is not None else None,
    )
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1].id) if len(rows) > limit else None
    return IdentityServiceAccountListOut(items=[_out(a) for a in page], next_cursor=next_cursor)


@router.get(
    "/service-accounts/{service_account_id}",
    response_model=IdentityServiceAccountOut,
    dependencies=[require("service-account:read", target=_service_account_target)],
    responses={
        401: {"description": "No valid token"},
        404: {"description": "Service account not found, or not visible to the caller"},
    },
)
async def get_service_account(
    service_account_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> IdentityServiceAccountOut:
    account = (
        await session.execute(select(ServiceAccount).where(ServiceAccount.id == service_account_id))
    ).scalar_one_or_none()
    if account is None:
        raise _not_found()
    return _out(account)


@router.patch(
    "/service-accounts/{service_account_id}",
    response_model=IdentityServiceAccountOut,
    dependencies=[require("service-account:manage", target=_service_account_target)],
    responses={
        401: {"description": "No valid token"},
        403: {
            "description": "The caller can read the service account but may not rename it, "
            "or the caller did not sign in interactively"
        },
        404: {"description": "Service account not found, or not visible to the caller"},
        409: {"description": "The name is taken under this owner, or the owner is archived"},
        422: {"description": "Validation error (only name may be changed)"},
    },
)
async def rename_service_account_route(
    service_account_id: uuid.UUID,
    body: IdentityServiceAccountPatchIn,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityServiceAccountOut:
    return _out(await rename_service_account(uow, principal, service_account_id, name=body.name))


@router.post(
    "/service-accounts/{service_account_id}/keys",
    status_code=201,
    response_model=IdentityServiceAccountKeyCreatedOut,
    dependencies=[require("service-account:manage-keys", target=_service_account_target)],
    responses={
        401: {"description": "No valid token"},
        403: {
            "description": "The caller can read the service account but may not manage its keys, "
            "or the caller did not sign in interactively"
        },
        404: {"description": "Service account not found, or not visible to the caller"},
        409: {
            "description": "The account already has two active keys, holds the effective maximum "
            "of active tokens, or is deactivated"
        },
        422: {"description": "Validation error, or the expiry is outside the allowed range"},
    },
)
async def create_key_route(
    service_account_id: uuid.UUID,
    body: IdentityServiceAccountKeyCreateIn,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityServiceAccountKeyCreatedOut:
    settings: IdentitySettings = request.app.state.settings
    policy = await request_policy(request)
    row, token = await create_service_account_key(
        uow,
        principal,
        service_account_id,
        name=body.name,
        expires_at=body.expires_at,
        settings=settings,
        max_days=policy.credential_max_ttl_days,
        max_active=policy.max_active_tokens,
    )
    response.headers["Location"] = (
        f"/api/v1/identity/service-accounts/{service_account_id}/keys/{row.id}"
    )
    return IdentityServiceAccountKeyCreatedOut(
        id=row.id, name=row.name, expires_at=row.expires_at, created_at=row.created_at, token=token
    )


@router.get(
    "/service-accounts/{service_account_id}/keys",
    response_model=IdentityServiceAccountKeyListOut,
    dependencies=[require("service-account:read", target=_service_account_target)],
    responses={
        401: {"description": "No valid token"},
        404: {"description": "Service account not found, or not visible to the caller"},
    },
)
async def list_keys_route(
    service_account_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> IdentityServiceAccountKeyListOut:
    settings: IdentitySettings = request.app.state.settings
    rows = await list_service_account_keys(session, service_account_id, settings)
    return IdentityServiceAccountKeyListOut(items=[_key_out(*row) for row in rows])


@router.delete(
    "/service-accounts/{service_account_id}/keys/{key_id}",
    status_code=204,
    dependencies=[require("service-account:manage-keys", target=_service_account_target)],
    responses={
        204: {"description": "The key is revoked (also when it already was)"},
        401: {"description": "No valid token"},
        403: {
            "description": "The caller can read the service account but may not manage its keys, "
            "or the caller did not sign in interactively"
        },
        404: {"description": "Service account or key not found, or not visible to the caller"},
    },
)
async def revoke_key_route(
    service_account_id: uuid.UUID,
    key_id: uuid.UUID,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> Response:
    await revoke_service_account_key(uow, principal, service_account_id, key_id)
    return Response(status_code=204)


_STATE_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "No valid token"},
    403: {
        "description": "The caller can read the service account but may not deactivate it, "
        "or the caller did not sign in interactively"
    },
    404: {"description": "Service account not found, or not visible to the caller"},
}


@router.post(
    "/service-accounts/{service_account_id}/deactivate",
    response_model=IdentityServiceAccountOut,
    dependencies=[require("service-account:deactivate", target=_service_account_target)],
    responses=_STATE_RESPONSES,
)
async def deactivate_service_account_route(
    service_account_id: uuid.UUID,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityServiceAccountOut:
    return _out(await deactivate_service_account(uow, principal, service_account_id))


@router.post(
    "/service-accounts/{service_account_id}/reactivate",
    response_model=IdentityServiceAccountOut,
    dependencies=[require("service-account:deactivate", target=_service_account_target)],
    responses=_STATE_RESPONSES,
)
async def reactivate_service_account_route(
    service_account_id: uuid.UUID,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityServiceAccountOut:
    return _out(await reactivate_service_account(uow, principal, service_account_id))


__all__ = [
    "IdentityServiceAccountCreateIn",
    "IdentityServiceAccountKeyCreateIn",
    "IdentityServiceAccountKeyCreatedOut",
    "IdentityServiceAccountKeyListOut",
    "IdentityServiceAccountKeyOut",
    "IdentityServiceAccountListOut",
    "IdentityServiceAccountOut",
    "IdentityServiceAccountPatchIn",
    "router",
]
