"""Group routes: ``/api/v1/identity/groups`` (RBAC-02, RBAC-08, D-27, D-33).

Groups are owned by ROOT or an Organization; ``group:create``, ``group:read`` and ``group:manage``
are evaluated at the owner folder, so an Organization admin reaches exactly their Organization's
groups and a caller who cannot read the owner gets 404 (identical to an unknown id). Membership
and grant ceilings are enforced inside the write transaction in :mod:`dc_identity.groups`.

Member wire shape: a member is added by exact ``username`` or by ``principal_id``
(``local:<uuid>`` or ``entra:<tid>:<oid>`` of a user, or ``sa:<uuid>`` of a service account, the
shapes of grantees in grant bodies), never by directory search. ``member_type`` is ``user`` or
``service_account`` and ``member_id`` is the member's principal id.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime
from typing import Annotated, Self

from dc_auth.deps import UnitOfWorkDep, authenticate, current_decision, require
from dc_auth.principal import Principal
from dc_auth.roles import Role, roles_with
from dc_core.app import read_session
from dc_core.names import NameRule
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from fastapi import Depends, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator
from sqlalchemy import Text, cast, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from dc_identity.access import (
    effective_roles,
    grantees_for,
    root_id,
    visible_folders_subquery,
)
from dc_identity.api.folders import _decode_cursor, _encode_cursor, _path_folder_id
from dc_identity.api.grants import PRINCIPAL_ID_PATTERNS
from dc_identity.grants import _not_found
from dc_identity.groups import (
    add_member,
    create_group,
    delete_group,
    group_owner,
    remove_member,
    rename_group,
)
from dc_identity.models import EntraUser, GroupMember, LocalGroup, ServiceAccount, User
from dc_identity.principal_ids import parse_principal_id
from dc_identity.text import NoNulStr

GLOBAL_ADMIN = Role.GLOBAL_ADMIN.value

router = PolicyRouter(prefix="/api/v1/identity")

GroupName = Annotated[str, StringConstraints(min_length=1, max_length=100), NameRule]


class IdentityGroupCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_folder_id: uuid.UUID
    name: GroupName


class IdentityGroupOut(BaseModel):
    id: uuid.UUID
    owner_folder_id: uuid.UUID
    name: str
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime


class IdentityGroupPatchIn(BaseModel):
    # Required and non-nullable: JSON null, a missing name and extra fields are all a 422.
    model_config = ConfigDict(extra="forbid")

    name: GroupName


class IdentityGroupListOut(BaseModel):
    items: list[IdentityGroupOut]
    next_cursor: str | None


class IdentityGroupMemberAddIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: NoNulStr | None = None
    principal_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.username is None) == (self.principal_id is None):
            raise ValueError("give exactly one of username and principal_id")
        if self.username is not None and not 1 <= len(self.username) <= 200:
            raise ValueError("username must be 1 to 200 characters")
        if self.principal_id is not None and (
            len(self.principal_id) > 200
            or PRINCIPAL_ID_PATTERNS["user"].fullmatch(self.principal_id) is None
        ):
            raise ValueError("principal_id does not have the shape of a principal id")
        return self


class IdentityGroupMemberOut(BaseModel):
    member_type: str
    member_id: str
    username: str | None
    display_name: str | None
    added_by: str
    added_at: datetime


class IdentityGroupMemberListOut(BaseModel):
    items: list[IdentityGroupMemberOut]
    next_cursor: str | None


class IdentityGrantableGroupOut(BaseModel):
    id: uuid.UUID
    owner_folder_id: uuid.UUID
    name: str


class IdentityGrantableGroupListOut(BaseModel):
    items: list[IdentityGrantableGroupOut]


def _out(group: LocalGroup) -> IdentityGroupOut:
    return IdentityGroupOut(
        id=group.id,
        owner_folder_id=group.owner_folder_id,
        name=group.name,
        created_by=group.created_by,
        created_at=group.created_at,
        updated_by=group.updated_by,
        updated_at=group.updated_at,
    )


def _member_out(
    member: GroupMember, username: str | None, display_name: str | None
) -> IdentityGroupMemberOut:
    return IdentityGroupMemberOut(
        member_type=member.member_type,
        member_id=member.member_id,
        username=username,
        display_name=display_name,
        added_by=member.created_by,
        added_at=member.created_at,
    )


async def _member_names(
    session: AsyncSession, member: GroupMember
) -> tuple[str | None, str | None]:
    """``(username, display_name)`` of a member: a user's, or a service account's name."""
    ref = parse_principal_id(member.member_id)
    if ref is None:
        return None, None
    if ref.kind == "local_user":
        user = await session.get(User, ref.local_id)
        return (user.username, user.display_name) if user else (None, None)
    if ref.kind == "entra":
        entra_user = (
            await session.execute(
                select(EntraUser).where(
                    EntraUser.tenant_id == ref.tenant_id, EntraUser.object_id == ref.object_id
                )
            )
        ).scalar_one_or_none()
        return (None, entra_user.display_name) if entra_user else (None, None)
    if ref.kind == "service_account":
        account = await session.get(ServiceAccount, ref.service_account_id)
        return (None, account.name) if account else (None, None)
    return None, None


async def _create_owner_id(body: IdentityGroupCreateIn) -> uuid.UUID:
    return body.owner_folder_id


async def _group_target(
    group_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> uuid.UUID:
    return await group_owner(group_id, session)


@router.post(
    "/groups",
    status_code=201,
    response_model=IdentityGroupOut,
    dependencies=[require("group:create", target=_create_owner_id)],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "The caller can read the owner folder but may not create groups"},
        404: {"description": "Owner folder not found, or not visible to the caller"},
        409: {"description": "The group name is taken here, or the owner is archived"},
        422: {"description": "Validation error, or the owner is not ROOT or an Organization"},
    },
)
async def create_group_route(
    body: IdentityGroupCreateIn,
    request: Request,
    response: Response,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityGroupOut:
    # Read inside the handler, not as a dependency (see create_folder_route).
    owner = current_decision(request).folder
    if owner is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    group = await create_group(uow, principal, owner, name=body.name)
    response.headers["Location"] = f"/api/v1/identity/groups/{group.id}"
    return _out(group)


@router.post(
    "/groups/{group_id}/members",
    status_code=201,
    response_model=IdentityGroupMemberOut,
    dependencies=[require("group:manage", target=_group_target)],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "Grant ceiling exceeded, or adding oneself"},
        404: {"description": "Group not found, or not visible to the caller"},
        409: {"description": "Already a member, or the owner is archived"},
        422: {
            "description": (
                "Validation error, the principal does not exist, or an Organization-owned "
                "service account is outside its Organization"
            )
        },
    },
)
async def add_member_route(
    group_id: uuid.UUID,
    body: IdentityGroupMemberAddIn,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityGroupMemberOut:
    member = await add_member(
        uow, principal, group_id, username=body.username, principal_id=body.principal_id
    )
    username, display_name = await _member_names(uow.session, member)
    return _member_out(member, username, display_name)


def _encode_member_cursor(member: GroupMember) -> str:
    raw = f"{member.member_type}\x1f{member.member_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_member_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        member_type, member_id = (
            base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8").split("\x1f", 1)
        )
    except ValueError, binascii.Error, UnicodeError:
        raise ProblemError(
            422,
            "validation-error",
            "Unprocessable Content",
            "The request failed validation.",
            errors=[{"pointer": "/query/cursor", "detail": "cursor is not valid"}],
        ) from None
    return member_type, member_id


@router.get(
    "/groups",
    response_model=IdentityGroupListOut,
    dependencies=[require("group:list")],
    responses={
        401: {"description": "No valid token"},
        422: {"description": "Invalid cursor"},
    },
)
async def list_groups(
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
    owner_folder_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> IdentityGroupListOut:
    stmt = select(LocalGroup)
    # Trim to the groups whose owner folder the caller may read groups at, in SQL. A Global admin
    # at ROOT sees every group; a caller with no access gets an empty page, never a 403 (D-35).
    grantees = grantees_for(principal)
    if GLOBAL_ADMIN not in await effective_roles(session, grantees, await root_id(session)):
        visible = visible_folders_subquery(grantees, roles_with("group:read"))
        stmt = stmt.where(LocalGroup.owner_folder_id.in_(select(visible.c.id)))
    if owner_folder_id is not None:
        stmt = stmt.where(LocalGroup.owner_folder_id == owner_folder_id)
    if cursor is not None:
        stmt = stmt.where(LocalGroup.id > _decode_cursor(cursor))
    stmt = stmt.order_by(LocalGroup.id).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars())
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1].id) if len(rows) > limit else None
    return IdentityGroupListOut(items=[_out(group) for group in page], next_cursor=next_cursor)


@router.get(
    "/groups/{group_id}",
    response_model=IdentityGroupOut,
    dependencies=[require("group:read", target=_group_target)],
    responses={
        401: {"description": "No valid token"},
        404: {"description": "Group not found, or not visible to the caller"},
    },
)
async def get_group(
    group_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> IdentityGroupOut:
    group = (
        await session.execute(select(LocalGroup).where(LocalGroup.id == group_id))
    ).scalar_one_or_none()
    if group is None:
        raise _not_found()
    return _out(group)


@router.patch(
    "/groups/{group_id}",
    response_model=IdentityGroupOut,
    dependencies=[require("group:manage", target=_group_target)],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "The caller can read the group but may not manage it"},
        404: {"description": "Group not found, or not visible to the caller"},
        409: {"description": "The name is taken by another group of this owner, or archived"},
        422: {"description": "Validation error (only name may be changed)"},
    },
)
async def rename_group_route(
    group_id: uuid.UUID,
    body: IdentityGroupPatchIn,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityGroupOut:
    return _out(await rename_group(uow, principal, group_id, name=body.name))


@router.delete(
    "/groups/{group_id}",
    status_code=204,
    dependencies=[require("group:manage", target=_group_target)],
    responses={
        204: {"description": "The group, its members and its role assignments were removed"},
        401: {"description": "No valid token"},
        403: {"description": "Grant ceiling exceeded for a role the group holds"},
        404: {"description": "Group not found, or not visible to the caller"},
    },
)
async def delete_group_route(
    group_id: uuid.UUID,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> Response:
    await delete_group(uow, principal, group_id)
    return Response(status_code=204)


@router.get(
    "/groups/{group_id}/members",
    response_model=IdentityGroupMemberListOut,
    dependencies=[require("group:read", target=_group_target)],
    responses={
        401: {"description": "No valid token"},
        404: {"description": "Group not found, or not visible to the caller"},
        422: {"description": "Invalid cursor"},
    },
)
async def list_members(
    group_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(read_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> IdentityGroupMemberListOut:
    stmt = (
        select(
            GroupMember,
            User.username,
            func.coalesce(User.display_name, EntraUser.display_name, ServiceAccount.name),
        )
        .outerjoin(
            User,
            (GroupMember.member_type == "user")
            & (GroupMember.member_id == func.concat("local:", cast(User.id, Text))),
        )
        .outerjoin(
            EntraUser,
            (GroupMember.member_type == "user")
            & (
                GroupMember.member_id
                == func.concat("entra:", EntraUser.tenant_id, ":", EntraUser.object_id)
            ),
        )
        .outerjoin(
            ServiceAccount,
            (GroupMember.member_type == "service_account")
            & (GroupMember.member_id == func.concat("sa:", cast(ServiceAccount.id, Text))),
        )
        .where(GroupMember.group_id == group_id)
    )
    if cursor is not None:
        stmt = stmt.where(
            tuple_(GroupMember.member_type, GroupMember.member_id) > _decode_member_cursor(cursor)
        )
    stmt = stmt.order_by(GroupMember.member_type, GroupMember.member_id).limit(limit + 1)
    rows = (await session.execute(stmt)).all()
    page = rows[:limit]
    next_cursor = _encode_member_cursor(page[-1][0]) if len(rows) > limit else None
    return IdentityGroupMemberListOut(
        items=[_member_out(row[0], row[1], row[2]) for row in page], next_cursor=next_cursor
    )


@router.delete(
    "/groups/{group_id}/members/{member_id}",
    status_code=204,
    dependencies=[require("group:manage", target=_group_target)],
    responses={
        204: {"description": "The member was removed"},
        401: {"description": "No valid token"},
        403: {"description": "Grant ceiling exceeded for a role the group holds"},
        404: {"description": "Group or member not found, or not visible to the caller"},
        422: {"description": "The member id is not a principal id"},
    },
)
async def remove_member_route(
    group_id: uuid.UUID,
    member_id: Annotated[str, Path(max_length=200, pattern=PRINCIPAL_ID_PATTERNS["user"].pattern)],
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> Response:
    await remove_member(uow, principal, group_id, member_id)
    return Response(status_code=204)


@router.get(
    "/folders/{folder_id}/grantable-groups",
    response_model=IdentityGrantableGroupListOut,
    dependencies=[require("grant:manage", target=_path_folder_id)],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "The caller can read the folder but may not manage its grants"},
        404: {"description": "Folder not found, or not visible to the caller"},
    },
)
async def grantable_groups(
    request: Request, session: Annotated[AsyncSession, Depends(read_session)]
) -> IdentityGrantableGroupListOut:
    """Groups an admin may grant at this folder: ROOT-owned and its Organization's.

    Accepted exposure: a grant manager without ``group:read`` at the owner (a Team admin) still
    sees these names, because the grant route accepts any group owned by an ancestor folder and
    this list is how they find one. Members are never shown. See docs/user/api-guide.md.
    """
    facts = current_decision(request).folder
    if facts is None:  # pragma: no cover - ALLOW with a target always carries the facts
        raise _not_found()
    owners = {facts.ancestor_ids[0]}
    if len(facts.ancestor_ids) >= 2:
        owners.add(facts.ancestor_ids[1])
    rows = (
        await session.execute(
            select(LocalGroup)
            .where(LocalGroup.owner_folder_id.in_(owners))
            .order_by(func.lower(LocalGroup.name), LocalGroup.id)
        )
    ).scalars()
    return IdentityGrantableGroupListOut(
        items=[
            IdentityGrantableGroupOut(id=g.id, owner_folder_id=g.owner_folder_id, name=g.name)
            for g in rows
        ]
    )


__all__ = [
    "IdentityGrantableGroupListOut",
    "IdentityGrantableGroupOut",
    "IdentityGroupCreateIn",
    "IdentityGroupListOut",
    "IdentityGroupMemberAddIn",
    "IdentityGroupMemberListOut",
    "IdentityGroupMemberOut",
    "IdentityGroupOut",
    "IdentityGroupPatchIn",
    "router",
]
