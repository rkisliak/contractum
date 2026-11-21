"""User routes: ``/api/v1/identity/users`` (RBAC-10, D-26).

Only a Global admin may create, list, read or rename users: ``user:create``, ``user:read`` and
``user:update`` are ROOT permissions with no target, so every other caller gets 403 (a
bootstrap-restricted session gets 403 ``password-rotation-required``, no token gets 401). A new
user holds no role assignment, so they see nothing until granted. The password is a
``SecretStr``: it is never echoed, logged or placed in an audit event, and the e-mail address is
stored but never copied into an event.
``PATCH /{user_id}`` (quick 261119-16f, ``user:update``, interactive sign-in only) changes a local
user's display name; an Entra user is not a local user, so its id is the ordinary 404.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from dc_auth.deps import UnitOfWorkDep, authenticate, require
from dc_auth.principal import Principal
from dc_core.app import read_session
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from fastapi import Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_identity.api.folders import _decode_cursor, _encode_cursor
from dc_identity.api.me import IdentityDisplayNameIn
from dc_identity.deactivation import deactivate_local_user, reactivate_local_user
from dc_identity.models import User
from dc_identity.passwords import PasswordService
from dc_identity.security_policy import request_policy
from dc_identity.text import DisplayName, NoNulStr
from dc_identity.users import (
    EMAIL_MAX_LENGTH,
    EMAIL_PATTERN,
    change_display_name,
    create_user,
    normalize_username,
)

router = PolicyRouter(prefix="/api/v1/identity/users")


class IdentityUserCreateIn(BaseModel):
    # extra="forbid": is_active, must_rotate and every other server-owned field are a 422 (T-02-21).
    model_config = ConfigDict(extra="forbid")

    # The exact username rules live in the domain service (USERNAME_PATTERN, after lowercasing);
    # the bound here only keeps absurd input out of the service.
    username: NoNulStr = Field(min_length=1, max_length=200)
    password: SecretStr = Field(min_length=1, max_length=1024)
    display_name: DisplayName
    email: NoNulStr | None = Field(default=None, max_length=EMAIL_MAX_LENGTH, pattern=EMAIL_PATTERN)


class IdentityUserOut(BaseModel):
    id: uuid.UUID
    principal_id: str
    username: str
    display_name: str
    email: str | None
    is_active: bool
    must_rotate: bool
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime


class IdentityUserListOut(BaseModel):
    items: list[IdentityUserOut]
    next_cursor: str | None


def _out(user: User) -> IdentityUserOut:
    return IdentityUserOut(
        id=user.id,
        principal_id=f"local:{user.id}",
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        is_active=user.is_active,
        must_rotate=user.must_rotate,
        created_by=user.created_by,
        created_at=user.created_at,
        updated_by=user.updated_by,
        updated_at=user.updated_at,
    )


@router.post(
    "",
    status_code=201,
    response_model=IdentityUserOut,
    dependencies=[require("user:create")],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "Only a Global admin may create users"},
        409: {"description": "A user with this username already exists"},
        422: {"description": "Validation error or password policy violation"},
    },
)
async def create_user_route(
    body: IdentityUserCreateIn, request: Request, response: Response, uow: UnitOfWorkDep
) -> IdentityUserOut:
    passwords: PasswordService = request.app.state.passwords
    user = await create_user(
        uow,
        passwords,
        username=body.username,
        password=body.password.get_secret_value(),
        display_name=body.display_name,
        email=body.email,
        rules=(await request_policy(request)).password_rules,
    )
    response.headers["Location"] = f"/api/v1/identity/users/{user.id}"
    return _out(user)


@router.get(
    "",
    response_model=IdentityUserListOut,
    dependencies=[require("user:read")],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "Only a Global admin may list users"},
        422: {"description": "Invalid cursor"},
    },
)
async def list_users(
    session: Annotated[AsyncSession, Depends(read_session)],
    username: Annotated[NoNulStr | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> IdentityUserListOut:
    stmt = select(User)
    if username is not None:
        stmt = stmt.where(User.username == normalize_username(username))
    if cursor is not None:
        stmt = stmt.where(User.id > _decode_cursor(cursor))
    stmt = stmt.order_by(User.id).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars())
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1].id) if len(rows) > limit else None
    return IdentityUserListOut(items=[_out(user) for user in page], next_cursor=next_cursor)


@router.get(
    "/{user_id}",
    response_model=IdentityUserOut,
    dependencies=[require("user:read")],
    responses={
        401: {"description": "No valid token"},
        403: {"description": "Only a Global admin may read users"},
        404: {"description": "User not found"},
    },
)
async def get_user(
    user_id: uuid.UUID, session: Annotated[AsyncSession, Depends(read_session)]
) -> IdentityUserOut:
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise ProblemError(404, "not-found", "Not Found", "The resource was not found.")
    return _out(user)


@router.patch(
    "/{user_id}",
    response_model=IdentityUserOut,
    dependencies=[require("user:update")],
    responses={
        401: {"description": "No valid token"},
        403: {
            "description": "Only a Global admin signed in interactively may change a user's "
            "display name (a PAT or key gets interactive-session-required)"
        },
        404: {"description": "User not found; Entra users are not local users"},
        422: {"description": "Validation error (blank, too long, control characters, extra field)"},
    },
)
async def update_user_display_name(
    user_id: uuid.UUID, body: IdentityDisplayNameIn, uow: UnitOfWorkDep
) -> IdentityUserOut:
    return _out(await change_display_name(uow, user_id, body.display_name))


@router.post(
    "/{user_id}/deactivate",
    response_model=IdentityUserOut,
    dependencies=[require("user:deactivate")],
    responses={
        401: {"description": "No valid token"},
        403: {
            "description": "Only a Global admin signed in interactively may deactivate users "
            "(a PAT or key gets interactive-session-required)"
        },
        404: {"description": "User not found"},
        409: {"description": "The last active Global admin cannot be deactivated"},
    },
)
async def deactivate_user(
    user_id: uuid.UUID,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityUserOut:
    return _out(await deactivate_local_user(uow, principal, user_id))


@router.post(
    "/{user_id}/reactivate",
    response_model=IdentityUserOut,
    dependencies=[require("user:deactivate")],
    responses={
        401: {"description": "No valid token"},
        403: {
            "description": "Only a Global admin signed in interactively may reactivate users "
            "(a PAT or key gets interactive-session-required)"
        },
        404: {"description": "User not found"},
    },
)
async def reactivate_user(
    user_id: uuid.UUID,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, Depends(authenticate)],
) -> IdentityUserOut:
    return _out(await reactivate_local_user(uow, principal, user_id))


__all__ = ["IdentityUserCreateIn", "IdentityUserListOut", "IdentityUserOut", "router"]
