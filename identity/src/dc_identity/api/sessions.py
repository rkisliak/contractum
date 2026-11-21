"""Active sessions of a local user (quick 261120-s0p, ASVS 7.4.5 and 7.5.2).

A session is a refresh-token family. ``GET /api/v1/identity/me/sessions`` lists the caller's own
live sessions (when it began, when it was last active, when it ends, a fixed-vocabulary device
summary such as "Chrome on macOS") and marks the current one. The current session is found from
the caller's access-token jti, which login and refresh store on the family's row, so the locked
token claim set does not change and the route does not depend on the refresh cookie (its path is
the auth router's). A PAT or key lists its owner's sessions and has none current; an Entra or
service-account principal has no local sessions and gets an empty list.

The list is a read: it is gated by ``me:read`` and writes no event. Session ids are family ids;
no token, hash, jti or User-Agent leaves the service.

Ending sessions: ``DELETE /me/sessions/{id}`` signs one out and ``POST /me/sessions/revoke-others``
every session but the current one (``me:update``: interactive only, so a PAT or key cannot end a
session). A Global admin lists a local user's sessions (``user:read``) and signs one or all of them
out (``user:deactivate``, interactive only; the session half of deactivation). The admin routes
address local users only: an unknown or Entra id is the ordinary 404. Every revocation is one
audited event and takes effect at the next refresh: an access token already issued stays valid
until it expires (``DC_ACCESS_TOKEN_TTL_S``, 15 minutes by default).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from dc_auth.deps import UnitOfWorkDep, require
from dc_auth.principal import Principal
from dc_core.app import read_session
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from fastapi import Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dc_identity.gate import resolved_for_request
from dc_identity.models import User
from dc_identity.principal_ids import parse_principal_id
from dc_identity.security_policy import request_policy
from dc_identity.sessions import (
    SessionList,
    family_of_jti,
    list_sessions,
    revoke_all_sessions,
    revoke_other_sessions,
    revoke_session,
)
from dc_identity.settings import IdentitySettings

me_router = PolicyRouter(prefix="/api/v1/identity/me/sessions")
users_router = PolicyRouter(prefix="/api/v1/identity/users")


class IdentitySessionOut(BaseModel):
    id: uuid.UUID
    started_at: datetime
    last_active_at: datetime
    idle_expires_at: datetime
    expires_at: datetime
    client: str | None
    current: bool


class IdentitySessionListOut(BaseModel):
    items: list[IdentitySessionOut]
    total: int
    max_sessions: int
    access_token_lifetime_s: int


class IdentitySessionsRevokedOut(BaseModel):
    revoked: int


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


def session_list_out(
    found: SessionList, *, max_sessions: int, access_token_lifetime_s: int
) -> IdentitySessionListOut:
    return IdentitySessionListOut(
        items=[
            IdentitySessionOut(
                id=item.family_id,
                started_at=item.started_at,
                last_active_at=item.last_active_at,
                idle_expires_at=item.idle_expires_at,
                expires_at=item.expires_at,
                client=item.client_summary,
                current=item.family_id == found.current_family_id,
            )
            for item in found.sessions
        ],
        total=found.total,
        max_sessions=max_sessions,
        access_token_lifetime_s=access_token_lifetime_s,
    )


async def local_user_id(request: Request, principal: Principal) -> uuid.UUID | None:
    """The caller's local user id (a PAT answers as its owner), or ``None`` for Entra and service
    accounts, which have no local sessions."""
    resolved = await resolved_for_request(request, principal)
    ref = parse_principal_id(resolved.grantee_id)
    if ref is None or ref.kind != "local_user":
        return None
    return ref.local_id


@me_router.get(
    "",
    response_model=IdentitySessionListOut,
    summary="List the caller's active sessions",
)
async def list_my_sessions(
    request: Request,
    principal: Annotated[Principal, require("me:read")],
    session: Annotated[AsyncSession, Depends(read_session)],
) -> IdentitySessionListOut:
    """The caller's live sessions, newest activity first, with the current one marked.

    Not audited. A session ends at its idle timeout, at the absolute session length or when it is
    signed out; ended sessions are not listed. ``max_sessions`` is the effective concurrent-session
    limit and ``access_token_lifetime_s`` how long an issued access token outlives a sign-out.
    """
    settings: IdentitySettings = request.app.state.settings
    policy = await request_policy(request)
    user_id = await local_user_id(request, principal)
    found = (
        SessionList(sessions=[], total=0, current_family_id=None)
        if user_id is None
        else await list_sessions(
            session,
            user_id,
            session_max_s=policy.session_max_s,
            current_jti=principal.credential_id if principal.via == "jwt" else None,
        )
    )
    return session_list_out(
        found,
        max_sessions=policy.max_sessions_per_user,
        access_token_lifetime_s=settings.access_token_ttl_s,
    )


_SIGN_OUT_RESPONSES: dict[int | str, dict[str, str]] = {
    404: {"description": "No such session of this account"},
}


@me_router.delete(
    "/{session_id}",
    status_code=204,
    summary="Sign one of the caller's sessions out",
    responses={
        **_SIGN_OUT_RESPONSES,
        204: {"description": "Signed out, or already ended"},
    },
)
async def revoke_my_session(
    session_id: uuid.UUID,
    request: Request,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, require("me:update")],
) -> Response:
    """End one session of the caller (``me:update``, interactive only). One ``session.revoked``
    event; an already ended session is 204 with none. It takes effect at the next refresh."""
    user_id = await local_user_id(request, principal)
    if user_id is None:
        raise _not_found()
    outcome = await revoke_session(uow, user_id, session_id, reason="user_revoked")
    if outcome == "unknown":
        raise _not_found()
    await uow.commit()
    return Response(status_code=204)


@me_router.post(
    "/revoke-others",
    response_model=IdentitySessionsRevokedOut,
    summary="Sign out every session except the current one",
    responses={
        409: {
            "description": (
                "`current-session-unknown`: the session making this request cannot be identified"
            )
        }
    },
)
async def revoke_my_other_sessions(
    request: Request,
    uow: UnitOfWorkDep,
    principal: Annotated[Principal, require("me:update")],
) -> IdentitySessionsRevokedOut:
    """End every live session of the caller except the one making this request.

    One ``user.sessions_revoked`` event when anything was revoked. A local sign-in whose session
    cannot be identified (its row predates session tracking) is 409 and nothing changes.
    """
    user_id = await local_user_id(request, principal)
    if user_id is None:
        return IdentitySessionsRevokedOut(revoked=0)
    current = await family_of_jti(
        uow.session, user_id, principal.credential_id if principal.via == "jwt" else None
    )
    if current is None:
        raise ProblemError(
            409,
            "current-session-unknown",
            "Conflict",
            "The session making this request cannot be identified; sign in again and retry.",
        )
    revoked = await revoke_other_sessions(
        uow, user_id, keep_family_id=current, reason="others_revoked"
    )
    await uow.commit()
    return IdentitySessionsRevokedOut(revoked=len(revoked))


async def _local_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise _not_found()
    return user


@users_router.get(
    "/{user_id}/sessions",
    dependencies=[require("user:read")],
    response_model=IdentitySessionListOut,
    summary="List a local user's active sessions",
    responses={404: {"description": "No such local user"}},
)
async def list_user_sessions(
    user_id: uuid.UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(read_session)],
) -> IdentitySessionListOut:
    """A Global admin's view of a local user's live sessions (not audited). The admin's own token
    is never one of the user's sessions, so none is current."""
    await _local_user(session, user_id)
    policy = await request_policy(request)
    settings: IdentitySettings = request.app.state.settings
    found = await list_sessions(
        session, user_id, session_max_s=policy.session_max_s, current_jti=None
    )
    return session_list_out(
        found,
        max_sessions=policy.max_sessions_per_user,
        access_token_lifetime_s=settings.access_token_ttl_s,
    )


@users_router.delete(
    "/{user_id}/sessions/{session_id}",
    dependencies=[require("user:deactivate")],
    status_code=204,
    summary="Sign one of a local user's sessions out",
    responses={
        **_SIGN_OUT_RESPONSES,
        204: {"description": "Signed out, or already ended"},
    },
)
async def revoke_user_session(
    user_id: uuid.UUID, session_id: uuid.UUID, uow: UnitOfWorkDep
) -> Response:
    """End one session of a local user (Global admin, interactive only). One ``session.revoked``
    event with reason ``admin_revoked``; takes effect at the user's next refresh."""
    await _local_user(uow.session, user_id)
    outcome = await revoke_session(uow, user_id, session_id, reason="admin_revoked")
    if outcome == "unknown":
        raise _not_found()
    await uow.commit()
    return Response(status_code=204)


@users_router.post(
    "/{user_id}/sessions/revoke-all",
    dependencies=[require("user:deactivate")],
    response_model=IdentitySessionsRevokedOut,
    summary="Sign out every session of a local user",
    responses={404: {"description": "No such local user"}},
)
async def revoke_all_user_sessions(
    user_id: uuid.UUID, uow: UnitOfWorkDep
) -> IdentitySessionsRevokedOut:
    """End every live session of a local user (Global admin, interactive only). One
    ``user.sessions_revoked`` event with reason ``admin_revoked_all`` when anything was revoked."""
    await _local_user(uow.session, user_id)
    revoked = await revoke_all_sessions(uow, user_id, reason="admin_revoked_all")
    await uow.commit()
    return IdentitySessionsRevokedOut(revoked=len(revoked))
