"""Entra registration routes: ``/api/v1/identity/entra/...`` (AUTH-03 to AUTH-05, D-57, D-59).

An administrator admits a whole Entra group, a single Entra user or a pipeline's service principal
here; registration grants nothing (D-57). Groups and service principals are owned by ROOT or an
Organization (D-59): ``entra:manage`` and ``entra:read`` are evaluated at the owner folder, so an
Organization admin reaches exactly their own Organization's registrations and a caller who cannot
read the owner gets 404, identical to an unknown id. Individual users are registered by a Global
admin only (``user:create`` at ROOT).

Every create body names an object GUID and may name a ``tenant_id``: omitted, it is the single
configured Entra tenant; with several configured it must be given (AUTH-03). The directory confirms
the object in that tenant before the write transaction opens (D-64): unknown is 422
``unknown-principal``, a directory outage 503, and nothing is stored in either case.

``GET /entra/directory/search`` (quick 261119-ofa) lets the register dialogs find a user, group or
service principal by name instead of by object id. Its gate is never weaker than registration:
``kind=user`` needs ``entra:manage`` at ROOT plus ``user:create`` (a Global admin, exactly the gate
of ``POST /entra/users``), ``group`` and ``service_principal`` need ``entra:manage`` at the chosen
ROOT or Organization owner. A read: no audit, the query never appears in a problem or a log.

``GET /entra/groups/{registration_id}/members`` (quick 261119-wp6) lists the group's transitive
members LIVE from Microsoft Graph, because Contractum never stores Entra membership. Its gate is
the group detail's: ``entra:read`` at the owner. A member that is already a Contractum user or
service principal carries the record id the UI links to, but only when the caller may open that
record (``user:read`` at ROOT, or ``entra:read`` at the service principal's owner).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from dc_auth.authorizer import AuthorizerUnavailable, Outcome
from dc_auth.deps import UnitOfWorkDep, authenticate, current_decision, require
from dc_auth.principal import Principal
from dc_core.app import read_session
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from fastapi import Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from dc_identity.access import root_id
from dc_identity.api.folders import _decode_cursor, _encode_cursor
from dc_identity.deactivation import (
    deactivate_entra_user,
    deactivate_service_principal,
    reactivate_entra_user,
    reactivate_service_principal,
)
from dc_identity.directory import (
    MEMBERS_LIMIT,
    SEARCH_MAX,
    SEARCH_MIN,
    DirectoryKind,
    DirectoryMemberKind,
    DirectorySearchKind,
    DirectoryUnavailable,
    normalize_search_query,
)
from dc_identity.entra import (
    check_owner_kind,
    entra_registration_owner,
    get_entra_user,
    known_group_members,
    list_entra_users,
    list_registrations,
    register_group,
    register_service_principal,
    register_user,
    registered_object_ids,
    resolve_tenant,
    unregister_group,
)
from dc_identity.gate import directory_unavailable
from dc_identity.grants import _not_found, _unknown_principal
from dc_identity.models import EntraRegistration, EntraUser
from dc_identity.platform_settings import default_entra_tenant
from dc_identity.principal_ids import GUID_PATTERN, entra_principal_id
from dc_identity.text import NoNulStr

router = PolicyRouter(prefix="/api/v1/identity")

# Anchored: pydantic's ``pattern`` searches, and only the canonical lowercase GUID is a principal.
_GUID = f"^{GUID_PATTERN.pattern}$"


class IdentityEntraGroupCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_folder_id: uuid.UUID
    object_id: NoNulStr = Field(pattern=_GUID)
    tenant_id: NoNulStr | None = Field(default=None, pattern=_GUID)


class IdentityEntraGroupOut(BaseModel):
    id: uuid.UUID
    tenant_id: str
    object_id: str
    owner_folder_id: uuid.UUID
    display_name: str
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime


class IdentityEntraGroupListOut(BaseModel):
    items: list[IdentityEntraGroupOut]
    next_cursor: str | None


def _group_out(row: EntraRegistration) -> IdentityEntraGroupOut:
    assert row.owner_folder_id is not None  # noqa: S101 - a group always has an owner
    return IdentityEntraGroupOut(
        id=row.id,
        tenant_id=row.tenant_id,
        object_id=row.object_id,
        owner_folder_id=row.owner_folder_id,
        display_name=row.display_name,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


async def _create_group_owner(body: IdentityEntraGroupCreateIn) -> uuid.UUID:
    return body.owner_folder_id


async def _group_registration_target(
    registration_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> uuid.UUID:
    return await entra_registration_owner(registration_id, session, kind="group")


@router.post(
    "/entra/groups",
    status_code=201,
    response_model=IdentityEntraGroupOut,
    dependencies=[require("entra:manage", target=_create_group_owner)],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "The caller can read the owner folder but may not register groups"},
        404: {"description": "Owner folder not found, or not visible to the caller"},
        409: {"description": "The group is already registered, or the owner is archived"},
        422: {
            "description": "Validation error, an unknown tenant, an unknown Entra group, "
            "or an owner that is not ROOT or an Organization"
        },
        503: {"description": "The directory is unavailable, or no Entra tenant is configured"},
    },
)
async def register_group_route(
    body: IdentityEntraGroupCreateIn,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityEntraGroupOut:
    owner = current_decision(request).folder
    if owner is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    check_owner_kind(owner)  # judged before the directory is asked
    tenant_id = resolve_tenant(
        request.app.state.entra_tenant_ids,
        body.tenant_id or await default_entra_tenant(request),
    )
    try:
        found = await request.app.state.directory.group(tenant_id, body.object_id)
    except DirectoryUnavailable as exc:
        raise directory_unavailable() from exc
    if found is None:
        raise _unknown_principal()
    row = await register_group(
        uow,
        principal,
        tenant_id=tenant_id,
        owner_folder_id=owner.id,
        object_id=body.object_id,
        display_name=found.display_name,
    )
    response.headers["Location"] = f"/api/v1/identity/entra/groups/{row.id}"
    return _group_out(row)


@router.get(
    "/entra/groups",
    response_model=IdentityEntraGroupListOut,
    dependencies=[require("entra:list")],
    responses={
        401: {"description": "No valid token"},
        422: {"description": "Invalid cursor"},
    },
)
async def list_group_registrations(
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> IdentityEntraGroupListOut:
    after = _decode_cursor(cursor) if cursor is not None else None
    rows = await list_registrations(session, principal, "group", limit=limit, after=after)
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1].id) if len(rows) > limit else None
    return IdentityEntraGroupListOut(items=[_group_out(r) for r in page], next_cursor=next_cursor)


@router.get(
    "/entra/groups/{registration_id}",
    response_model=IdentityEntraGroupOut,
    dependencies=[require("entra:read", target=_group_registration_target)],
    responses={
        401: {"description": "No valid token"},
        404: {"description": "Registration not found, or not visible to the caller"},
    },
)
async def get_group_registration(
    registration_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> IdentityEntraGroupOut:
    row = await session.get(EntraRegistration, registration_id)
    if row is None or row.kind != "group":
        raise _not_found()
    return _group_out(row)


@router.delete(
    "/entra/groups/{registration_id}",
    status_code=204,
    dependencies=[require("entra:manage", target=_group_registration_target)],
    responses={
        204: {"description": "The registration and every grant made to its GUID were removed"},
        401: {"description": "No valid token"},
        403: {"description": "Grant ceiling exceeded for a role granted to the group"},
        404: {"description": "Registration not found, or not visible to the caller"},
    },
)
async def delete_group_registration(
    registration_id: uuid.UUID,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> Response:
    await unregister_group(uow, principal, registration_id)
    return Response(status_code=204)


# --- Service principals -------------------------------------------------------------------------


class IdentityEntraServicePrincipalCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_folder_id: uuid.UUID
    object_id: NoNulStr = Field(pattern=_GUID)
    tenant_id: NoNulStr | None = Field(default=None, pattern=_GUID)


class IdentityEntraServicePrincipalOut(IdentityEntraGroupOut):
    principal_id: str
    is_active: bool
    directly_registered: bool


class IdentityEntraServicePrincipalListOut(BaseModel):
    items: list[IdentityEntraServicePrincipalOut]
    next_cursor: str | None


def _sp_out(row: EntraRegistration) -> IdentityEntraServicePrincipalOut:
    assert row.owner_folder_id is not None  # noqa: S101 - a service principal always has an owner
    return IdentityEntraServicePrincipalOut(
        id=row.id,
        tenant_id=row.tenant_id,
        object_id=row.object_id,
        owner_folder_id=row.owner_folder_id,
        display_name=row.display_name,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
        principal_id=entra_principal_id(row.tenant_id, row.object_id),
        is_active=row.is_active,
        directly_registered=row.directly_registered,
    )


async def _create_sp_owner(body: IdentityEntraServicePrincipalCreateIn) -> uuid.UUID:
    return body.owner_folder_id


async def _sp_registration_target(
    registration_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> uuid.UUID:
    return await entra_registration_owner(registration_id, session, kind="service_principal")


async def _confirm_in_directory(
    request: Request, tenant_id: str, kind: DirectoryKind, object_id: str
) -> str:
    """The directory's display name for a user or service principal, or the matching problem."""
    try:
        entry = await request.app.state.directory.lookup(tenant_id, kind, object_id)
    except DirectoryUnavailable as exc:
        raise directory_unavailable() from exc
    if not entry.found:
        raise _unknown_principal()
    return str(entry.display_name or object_id)


@router.post(
    "/entra/service-principals",
    status_code=201,
    response_model=IdentityEntraServicePrincipalOut,
    dependencies=[require("entra:manage", target=_create_sp_owner)],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "The caller can read the owner folder but may not register"},
        404: {"description": "Owner folder not found, or not visible to the caller"},
        409: {
            "description": "Already registered directly, owned by another owner, "
            "or the owner is archived"
        },
        422: {
            "description": "Validation error, an unknown tenant or service principal, "
            "or an owner that is not ROOT or an Organization"
        },
        503: {"description": "The directory is unavailable, or no Entra tenant is configured"},
    },
)
async def register_service_principal_route(
    body: IdentityEntraServicePrincipalCreateIn,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityEntraServicePrincipalOut:
    owner = current_decision(request).folder
    if owner is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    check_owner_kind(owner)
    tenant_id = resolve_tenant(
        request.app.state.entra_tenant_ids,
        body.tenant_id or await default_entra_tenant(request),
    )
    name = await _confirm_in_directory(request, tenant_id, "service_principal", body.object_id)
    row = await register_service_principal(
        uow,
        principal,
        tenant_id=tenant_id,
        owner_folder_id=owner.id,
        object_id=body.object_id,
        display_name=name,
    )
    response.headers["Location"] = f"/api/v1/identity/entra/service-principals/{row.id}"
    return _sp_out(row)


@router.get(
    "/entra/service-principals",
    response_model=IdentityEntraServicePrincipalListOut,
    dependencies=[require("entra:list")],
    responses={
        401: {"description": "No valid token"},
        422: {"description": "Invalid cursor"},
    },
)
async def list_service_principal_registrations(
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> IdentityEntraServicePrincipalListOut:
    after = _decode_cursor(cursor) if cursor is not None else None
    rows = await list_registrations(
        session, principal, "service_principal", limit=limit, after=after
    )
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1].id) if len(rows) > limit else None
    return IdentityEntraServicePrincipalListOut(
        items=[_sp_out(r) for r in page], next_cursor=next_cursor
    )


@router.get(
    "/entra/service-principals/{registration_id}",
    response_model=IdentityEntraServicePrincipalOut,
    dependencies=[require("entra:read", target=_sp_registration_target)],
    responses={
        401: {"description": "No valid token"},
        404: {"description": "Registration not found, or not visible to the caller"},
    },
)
async def get_service_principal_registration(
    registration_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> IdentityEntraServicePrincipalOut:
    row = await session.get(EntraRegistration, registration_id)
    if row is None or row.kind != "service_principal":
        raise _not_found()
    return _sp_out(row)


_SP_STATE_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "No valid token"},
    403: {
        "description": "The caller can read the registration but may not manage it, "
        "or the caller did not sign in interactively"
    },
    404: {"description": "Registration not found, or not visible to the caller"},
}


@router.post(
    "/entra/service-principals/{registration_id}/deactivate",
    response_model=IdentityEntraServicePrincipalOut,
    dependencies=[require("entra:manage", target=_sp_registration_target)],
    responses=_SP_STATE_RESPONSES,
)
async def deactivate_service_principal_route(
    registration_id: uuid.UUID,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityEntraServicePrincipalOut:
    return _sp_out(await deactivate_service_principal(uow, principal, registration_id))


@router.post(
    "/entra/service-principals/{registration_id}/reactivate",
    response_model=IdentityEntraServicePrincipalOut,
    dependencies=[require("entra:manage", target=_sp_registration_target)],
    responses=_SP_STATE_RESPONSES,
)
async def reactivate_service_principal_route(
    registration_id: uuid.UUID,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityEntraServicePrincipalOut:
    return _sp_out(await reactivate_service_principal(uow, principal, registration_id))


# --- Users --------------------------------------------------------------------------------------


class IdentityEntraUserCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_id: NoNulStr = Field(pattern=_GUID)
    tenant_id: NoNulStr | None = Field(default=None, pattern=_GUID)


class IdentityEntraUserOut(BaseModel):
    id: uuid.UUID
    principal_id: str
    tenant_id: str
    object_id: str
    display_name: str
    preferred_username: str | None
    is_active: bool
    registered_directly: bool
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime


class IdentityEntraUserListOut(BaseModel):
    items: list[IdentityEntraUserOut]
    next_cursor: str | None


def _user_out(user: EntraUser, registered_directly: bool) -> IdentityEntraUserOut:
    return IdentityEntraUserOut(
        id=user.id,
        principal_id=entra_principal_id(user.tenant_id, user.object_id),
        tenant_id=user.tenant_id,
        object_id=user.object_id,
        display_name=user.display_name,
        preferred_username=user.preferred_username,
        is_active=user.is_active,
        registered_directly=registered_directly,
        created_by=user.created_by,
        created_at=user.created_at,
        updated_by=user.updated_by,
        updated_at=user.updated_at,
    )


@router.post(
    "/entra/users",
    status_code=201,
    response_model=IdentityEntraUserOut,
    dependencies=[require("user:create")],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "Only a Global admin may register Entra users"},
        409: {"description": "The Entra user is already registered"},
        422: {"description": "Validation error, an unknown tenant or an unknown Entra user"},
        503: {"description": "The directory is unavailable, or no Entra tenant is configured"},
    },
)
async def register_user_route(
    body: IdentityEntraUserCreateIn,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityEntraUserOut:
    tenant_id = resolve_tenant(
        request.app.state.entra_tenant_ids,
        body.tenant_id or await default_entra_tenant(request),
    )
    name = await _confirm_in_directory(request, tenant_id, "user", body.object_id)
    user = await register_user(
        uow, principal, tenant_id=tenant_id, object_id=body.object_id, display_name=name
    )
    response.headers["Location"] = f"/api/v1/identity/entra/users/{user.id}"
    return _user_out(user, True)


@router.get(
    "/entra/users",
    response_model=IdentityEntraUserListOut,
    dependencies=[require("user:read")],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "Only a Global admin may list Entra users"},
        422: {"description": "Invalid cursor"},
    },
)
async def list_entra_user_records(
    session: Annotated[AsyncSession, Depends(read_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> IdentityEntraUserListOut:
    after = _decode_cursor(cursor) if cursor is not None else None
    rows = await list_entra_users(session, limit=limit, after=after)
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1][0].id) if len(rows) > limit else None
    return IdentityEntraUserListOut(
        items=[_user_out(user, direct) for user, direct in page], next_cursor=next_cursor
    )


@router.get(
    "/entra/users/{entra_user_id}",
    response_model=IdentityEntraUserOut,
    dependencies=[require("user:read")],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "Only a Global admin may read Entra users"},
        404: {"description": "Entra user not found"},
    },
)
async def get_entra_user_record(
    entra_user_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> IdentityEntraUserOut:
    found = await get_entra_user(session, entra_user_id)
    if found is None:
        raise _not_found()
    return _user_out(*found)


_USER_STATE_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "No valid token"},
    403: {
        "description": "Only a Global admin signed in interactively may deactivate or "
        "reactivate Entra users"
    },
    404: {"description": "Entra user not found"},
}


async def _user_after_state_change(session: AsyncSession, user: EntraUser) -> IdentityEntraUserOut:
    found = await get_entra_user(session, user.id)
    if found is None:  # pragma: no cover - the row was just written
        raise _not_found()
    return _user_out(*found)


@router.post(
    "/entra/users/{entra_user_id}/deactivate",
    response_model=IdentityEntraUserOut,
    dependencies=[require("user:deactivate")],
    responses=_USER_STATE_RESPONSES,
)
async def deactivate_entra_user_route(
    entra_user_id: uuid.UUID,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
) -> IdentityEntraUserOut:
    user = await deactivate_entra_user(uow, principal, entra_user_id)
    return await _user_after_state_change(session, user)


@router.post(
    "/entra/users/{entra_user_id}/reactivate",
    response_model=IdentityEntraUserOut,
    dependencies=[require("user:deactivate")],
    responses=_USER_STATE_RESPONSES,
)
async def reactivate_entra_user_route(
    entra_user_id: uuid.UUID,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
) -> IdentityEntraUserOut:
    user = await reactivate_entra_user(uow, principal, entra_user_id)
    return await _user_after_state_change(session, user)


# --- Directory search ---------------------------------------------------------------------------


class IdentityEntraDirectoryMatchOut(BaseModel):
    object_id: str
    tenant_id: str
    display_name: str
    secondary: str | None
    kind: DirectorySearchKind
    already_registered: bool


class IdentityEntraDirectorySearchOut(BaseModel):
    items: list[IdentityEntraDirectoryMatchOut]
    truncated: bool


class _SearchParams(BaseModel):
    kind: DirectorySearchKind
    q: str
    tenant_id: str | None
    owner_folder_id: uuid.UUID | None


async def _search_params(
    kind: Annotated[DirectorySearchKind, Query(description="What to search for")],
    q: Annotated[
        str,
        Query(
            min_length=SEARCH_MIN,
            max_length=SEARCH_MAX,
            description="Name, email, user principal name or application ID; or an object ID",
        ),
    ],
    tenant_id: Annotated[
        str | None, Query(pattern=_GUID, description="Entra tenant (default: the configured one)")
    ] = None,
    owner_folder_id: Annotated[
        uuid.UUID | None,
        Query(description="The ROOT or Organization that would own the registration"),
    ] = None,
) -> _SearchParams:
    return _SearchParams(kind=kind, q=q, tenant_id=tenant_id, owner_folder_id=owner_folder_id)


async def _directory_search_target(
    params: Annotated[_SearchParams, Depends(_search_params)],
    session: Annotated[AsyncSession, Depends(read_session)],
) -> uuid.UUID:
    if params.kind == "user":
        return await root_id(session)
    if params.owner_folder_id is None:
        raise ProblemError(
            422,
            "validation-error",
            "Unprocessable Content",
            "owner_folder_id is required for groups and service principals.",
        )
    return params.owner_folder_id


@router.get(
    "/entra/directory/search",
    response_model=IdentityEntraDirectorySearchOut,
    dependencies=[require("entra:manage", target=_directory_search_target)],
    responses={
        401: {"description": "No valid token"},
        403: {
            "description": "The caller may not register this kind of principal, "
            "or did not sign in interactively"
        },
        404: {"description": "Owner folder not found, or not visible to the caller"},
        422: {
            "description": "A query with fewer than 2 searchable characters, a missing owner, "
            "an unknown tenant, or an owner that is not ROOT or an Organization"
        },
        503: {"description": "The directory is unavailable, or no Entra tenant is configured"},
    },
)
async def search_entra_directory(
    params: Annotated[_SearchParams, Depends(_search_params)],
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
) -> IdentityEntraDirectorySearchOut:
    """Up to 20 directory objects matching the query, each flagged when already registered."""
    owner_id: uuid.UUID | None = None
    if params.kind == "user":
        # The same gate as POST /entra/users, whatever the role matrix grants entra:manage.
        authorizer = request.app.state.authorizer_factory(request)
        try:
            decision = await authorizer.check(principal, "user:create", None)
        except AuthorizerUnavailable as exc:
            raise ProblemError(
                503,
                "service-unavailable",
                "Service Unavailable",
                "Authorization is temporarily unavailable.",
            ) from exc
        if decision.outcome is Outcome.NOT_FOUND:
            raise _not_found()
        if decision.outcome is not Outcome.ALLOW:
            raise ProblemError(
                403, "forbidden", "Forbidden", "You are not permitted to perform this action."
            )
    else:
        owner = current_decision(request).folder
        if owner is None:  # pragma: no cover - ALLOW with a target always carries the facts
            raise _not_found()
        check_owner_kind(owner)
        owner_id = owner.id
    try:
        query = normalize_search_query(params.q)
    except ValueError:
        raise ProblemError(
            422,
            "validation-error",
            "Unprocessable Content",
            "q needs at least 2 searchable characters.",
        ) from None
    tenant_id = resolve_tenant(
        request.app.state.entra_tenant_ids,
        params.tenant_id or await default_entra_tenant(request),
    )
    try:
        found = await request.app.state.directory.search(tenant_id, params.kind, query)
    except DirectoryUnavailable as exc:
        raise directory_unavailable() from exc
    taken = await registered_object_ids(
        session,
        params.kind,
        tenant_id,
        [m.object_id for m in found.items],
        owner_id,
    )
    return IdentityEntraDirectorySearchOut(
        items=[
            IdentityEntraDirectoryMatchOut(
                object_id=m.object_id,
                tenant_id=tenant_id,
                display_name=m.display_name,
                secondary=m.secondary,
                kind=m.kind,
                already_registered=m.object_id in taken,
            )
            for m in found.items
        ],
        truncated=found.truncated,
    )


# --- Live group members -------------------------------------------------------------------------


class IdentityEntraGroupMemberOut(BaseModel):
    object_id: str
    display_name: str
    secondary: str | None
    kind: DirectoryMemberKind
    account_enabled: bool | None
    registered: bool
    principal_id: str | None
    record_id: uuid.UUID | None


class IdentityEntraGroupMembersOut(BaseModel):
    items: list[IdentityEntraGroupMemberOut]
    truncated: bool
    total: int | None


@router.get(
    "/entra/groups/{registration_id}/members",
    response_model=IdentityEntraGroupMembersOut,
    dependencies=[require("entra:read", target=_group_registration_target)],
    responses={
        401: {"description": "No valid token"},
        404: {
            "description": "Registration not found or not visible to the caller, "
            "or the group no longer exists in the directory"
        },
        503: {"description": "The directory is unavailable, or no Entra tenant is configured"},
    },
)
async def list_group_members(
    registration_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
) -> IdentityEntraGroupMembersOut:
    """The group's members as Microsoft Entra ID reports them now (at most 200, nested included)."""
    row = await session.get(EntraRegistration, registration_id)
    if row is None or row.kind != "group":
        raise _not_found()
    try:
        found = await request.app.state.directory.group_members(
            row.tenant_id, row.object_id, limit=MEMBERS_LIMIT
        )
    except DirectoryUnavailable as exc:
        raise directory_unavailable() from exc
    if found is None:
        raise _not_found()
    known = await known_group_members(
        session,
        principal,
        row.tenant_id,
        user_ids=[m.object_id for m in found.items if m.kind == "user"],
        service_principal_ids=[m.object_id for m in found.items if m.kind == "service_principal"],
    )
    items = []
    for m in found.items:
        record_id = known.get((m.kind, m.object_id))
        items.append(
            IdentityEntraGroupMemberOut(
                object_id=m.object_id,
                display_name=m.display_name,
                secondary=m.secondary,
                kind=m.kind,
                account_enabled=m.account_enabled,
                registered=record_id is not None,
                principal_id=(
                    entra_principal_id(row.tenant_id, m.object_id)
                    if record_id is not None
                    else None
                ),
                record_id=record_id,
            )
        )
    return IdentityEntraGroupMembersOut(items=items, truncated=found.truncated, total=found.total)


__all__ = [
    "IdentityEntraDirectoryMatchOut",
    "IdentityEntraDirectorySearchOut",
    "IdentityEntraGroupCreateIn",
    "IdentityEntraGroupListOut",
    "IdentityEntraGroupMemberOut",
    "IdentityEntraGroupMembersOut",
    "IdentityEntraGroupOut",
    "IdentityEntraServicePrincipalCreateIn",
    "IdentityEntraServicePrincipalListOut",
    "IdentityEntraServicePrincipalOut",
    "IdentityEntraUserCreateIn",
    "IdentityEntraUserListOut",
    "IdentityEntraUserOut",
    "router",
]
