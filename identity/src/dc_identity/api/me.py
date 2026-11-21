"""Self-description routes: ``GET /api/v1/identity/me`` and its ``/capabilities`` sibling.

D-UI-08-19, D-UI-08-20, D-UI-08-21 (UI-07). Any authenticated, non-restricted principal (JWT, PAT or
service-account key) asks who it is and what it may do. The answers inform the UI only: the server
stays the authority and every write re-checks, so a capability list that is stale or ignored can
never grant anything.

* ``GET /me`` answers ``IdentityMeOut``. ``principal_id`` is the grant form of the id (a PAT
  answers with its owner's id), ``principal_type`` is derived from the resolved grantee and never
  ``pat`` or ``system``, and ``assignments`` are trimmed to the folders the caller may read (D-35).
* ``GET /me/capabilities`` answers the roles, the folder-evaluated permissions after the PDP's own
  narrowing (read-only PAT, D-66; interactive-only, D-79; both in :mod:`dc_identity.authz`) and the
  roles the caller could grant there (the grant write's own predicates, :mod:`dc_identity.guards`).
  With ``folder_id`` the folder is first checked through the PDP as ``folder:read``: every refusal
  is the uniform 404 and the denial is recorded, so the route never confirms a folder exists.
  Without it the answer is the ROOT roles and the same global permissions as ``/me``.

Both routes are gated by the principal-level ``me:read``, which every non-restricted principal
holds; a restricted bootstrap token gets 403 ``password-rotation-required``. Reads are not audited.

* ``PATCH /me`` (quick 261119-16f) changes the caller's own display name. It is the one write here
  and is audited (one ``user.display_name_changed`` per effective change). It needs the
  principal-level, interactive-only ``me:update``: a PAT or key gets 403
  ``interactive-session-required``. Only a local user may use it: an Entra principal's name comes
  from Microsoft Entra ID (409 ``display-name-managed-externally``, nothing written) and a service
  account is renamed through its own route.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Annotated, Literal

from dc_auth.authorizer import AuthorizerUnavailable, FolderFacts, Outcome
from dc_auth.deps import UnitOfWorkDep, authenticate, require
from dc_auth.principal import Principal
from dc_core.app import read_session
from dc_core.policy import PolicyRouter
from dc_core.problems import ProblemError
from fastapi import Depends, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from dc_identity.access import (
    effective_roles,
    grantee_set,
    grantees_for,
    inheritance_cut,
    root_id,
)
from dc_identity.authz import (
    folder_capabilities,
    global_permissions_for,
    is_interactive,
    load_folder_facts,
)
from dc_identity.folders import ltree_to_slug_path
from dc_identity.gate import ResolvedPrincipal, resolved_for_request
from dc_identity.guards import grantable_roles
from dc_identity.models import (
    EntraRegistration,
    EntraUser,
    Folder,
    LocalGroup,
    RoleAssignment,
    ServiceAccount,
    User,
)
from dc_identity.principal_ids import PrincipalRef, parse_principal_id
from dc_identity.text import DisplayName
from dc_identity.users import change_display_name

router = PolicyRouter(prefix="/api/v1/identity/me")

_VIA: dict[str, Literal["jwt", "pat", "key"]] = {"jwt": "jwt", "pat": "pat", "api_key": "key"}
_LOCAL_GROUP = "local_group"
_ENTRA_GROUP = "entra_group"


class IdentityMeGroupOut(BaseModel):
    id: str
    type: Literal["local_group", "entra_group"]
    display_name: str


class IdentityMeAssignmentOut(BaseModel):
    grant_id: uuid.UUID
    folder_id: uuid.UUID
    folder_path: str
    role: str
    via_group_id: str | None


class IdentityMeOut(BaseModel):
    principal_id: str
    principal_type: Literal["user", "entra_user", "service_account", "service_principal"]
    idp: Literal["local", "entra"]
    via: Literal["jwt", "pat", "key"]
    display_name: str
    username: str | None
    preferred_username: str | None
    read_only: bool
    interactive: bool
    groups: list[IdentityMeGroupOut]
    assignments: list[IdentityMeAssignmentOut]
    global_permissions: list[str]


class IdentityMeCapabilitiesOut(BaseModel):
    folder_id: uuid.UUID | None
    roles: list[str]
    permissions: list[str]
    inheritance_blocked: bool
    grantable_roles: list[str]


class IdentityDisplayNameIn(BaseModel):
    # extra="forbid": any other field is a 422 (mass assignment, T-261119-16f-06).
    model_config = ConfigDict(extra="forbid")

    display_name: DisplayName


class IdentityProfileOut(BaseModel):
    principal_id: str
    display_name: str
    username: str
    updated_at: datetime


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


def _unavailable() -> ProblemError:
    return ProblemError(
        503,
        "service-unavailable",
        "Service Unavailable",
        "Authorization is temporarily unavailable.",
    )


async def _resolved(request: Request, principal: Principal) -> ResolvedPrincipal:
    """The gate's answer for this request (``me:read`` already ran it), resolving if absent."""
    return await resolved_for_request(request, principal)


def _principal_type(
    ref: PrincipalRef | None, resolved: ResolvedPrincipal
) -> Literal["user", "entra_user", "service_account", "service_principal"]:
    """Derived from the resolved grantee (a PAT answers as its owner); never ``pat``/``system``."""
    if ref is not None and ref.kind == "service_account":
        return "service_account"
    if resolved.grantee_type == "service_principal":
        return "service_principal"
    if ref is not None and ref.kind == "entra":
        return "entra_user"
    return "user"


async def _identity_names(
    session: AsyncSession, ref: PrincipalRef | None, principal: Principal
) -> tuple[str, str | None, str | None]:
    """``(display_name, username, preferred_username)`` of the resolved account's own row."""
    display = principal.display
    username: str | None = None
    preferred: str | None = None
    if ref is None:
        return display, None, None
    if ref.kind == "local_user":
        user = await session.get(User, ref.local_id)
        if user is not None:
            return user.display_name, user.username, None
    elif ref.kind == "service_account":
        account = await session.get(ServiceAccount, ref.service_account_id)
        if account is not None:
            return account.name, None, None
    else:
        assert ref.tenant_id is not None and ref.object_id is not None  # noqa: S101
        entra_user = await session.scalar(
            select(EntraUser).where(
                EntraUser.tenant_id == ref.tenant_id, EntraUser.object_id == ref.object_id
            )
        )
        if entra_user is not None:
            display = entra_user.display_name
            preferred = entra_user.preferred_username
        else:
            registration = await session.scalar(
                select(EntraRegistration).where(
                    EntraRegistration.kind == "service_principal",
                    EntraRegistration.tenant_id == ref.tenant_id,
                    EntraRegistration.object_id == ref.object_id,
                )
            )
            if registration is not None:
                display = registration.display_name
        # The token's own hint is display only (D-81) and is newer than the stored copy.
        if principal.username_hint and principal.type == "user":
            preferred = principal.username_hint
    return display, username, preferred


async def _groups(
    session: AsyncSession, members: frozenset[tuple[str, str]]
) -> list[IdentityMeGroupOut]:
    local_ids = [uuid.UUID(pid) for ptype, pid in members if ptype == _LOCAL_GROUP]
    entra_ids = [pid for ptype, pid in members if ptype == _ENTRA_GROUP]
    out: list[IdentityMeGroupOut] = []
    if local_ids:
        rows = (
            await session.execute(
                select(LocalGroup.id, LocalGroup.name).where(LocalGroup.id.in_(local_ids))
            )
        ).all()
        out.extend(
            IdentityMeGroupOut(id=str(gid), type="local_group", display_name=name)
            for gid, name in rows
        )
    if entra_ids:
        # Only registered Entra groups can carry a grant (D-64); the rest have no platform meaning.
        registered = (
            await session.execute(
                select(EntraRegistration.object_id, EntraRegistration.display_name)
                .where(
                    EntraRegistration.kind == "group",
                    EntraRegistration.object_id.in_(entra_ids),
                )
                .order_by(EntraRegistration.id)
            )
        ).all()
        seen: set[str] = set()
        for object_id, name in registered:
            if object_id in seen:
                continue
            seen.add(object_id)
            out.append(IdentityMeGroupOut(id=object_id, type="entra_group", display_name=name))
    return sorted(out, key=lambda group: (group.type, group.display_name.lower(), group.id))


async def _ancestors(
    session: AsyncSession, folder_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[uuid.UUID, ...]]:
    """Ancestors-or-self of each folder, root first, in one query."""
    target = aliased(Folder)
    ancestor = aliased(Folder)
    rows = (
        await session.execute(
            select(target.id, ancestor.id)
            .join(ancestor, ancestor.path.op("@>")(target.path))
            .where(target.id.in_(folder_ids))
            .order_by(target.id, func.nlevel(ancestor.path))
        )
    ).all()
    chains: dict[uuid.UUID, list[uuid.UUID]] = {}
    for folder_id, ancestor_id in rows:
        chains.setdefault(folder_id, []).append(ancestor_id)
    return {folder_id: tuple(chain) for folder_id, chain in chains.items()}


async def _assignments(
    session: AsyncSession,
    members: frozenset[tuple[str, str]],
    covers: Callable[[uuid.UUID, Sequence[uuid.UUID]], bool],
) -> list[IdentityMeAssignmentOut]:
    rows = (
        await session.execute(
            select(RoleAssignment, Folder.path)
            .join(Folder, Folder.id == RoleAssignment.folder_id)
            .where(
                tuple_(RoleAssignment.principal_type, RoleAssignment.principal_id).in_(
                    sorted(members)
                )
            )
        )
    ).all()
    if not rows:
        return []
    chains = await _ancestors(session, {grant.folder_id for grant, _ in rows})
    out: list[IdentityMeAssignmentOut] = []
    for grant, path in rows:
        chain = chains.get(grant.folder_id, (grant.folder_id,))
        if not covers(grant.folder_id, chain):
            continue  # D-35: a folder the caller cannot read is not even named
        out.append(
            IdentityMeAssignmentOut(
                grant_id=grant.id,
                folder_id=grant.folder_id,
                folder_path=ltree_to_slug_path(str(path)),
                role=grant.role,
                via_group_id=(
                    grant.principal_id
                    if grant.principal_type in (_LOCAL_GROUP, _ENTRA_GROUP)
                    else None
                ),
            )
        )
    return sorted(out, key=lambda item: (item.folder_path, item.role, str(item.grant_id)))


@router.get(
    "",
    response_model=IdentityMeOut,
    dependencies=[require("me:read")],
    responses={
        401: {"description": "Missing or invalid credentials"},
        403: {"description": "Restricted bootstrap session (password rotation required)"},
    },
)
async def get_me(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
) -> IdentityMeOut:
    """Who the caller is, its groups, its role assignments and its global permissions."""
    via = _VIA.get(str(principal.via))
    if via is None:
        raise ProblemError(
            403, "forbidden", "Forbidden", "You are not permitted to perform this action."
        )
    resolved = await _resolved(request, principal)
    ref = parse_principal_id(resolved.grantee_id)
    grantees = grantees_for(principal)
    members = await grantee_set(session, grantees)
    display_name, username, preferred_username = await _identity_names(session, ref, principal)
    authorizer = request.app.state.authorizer_factory(request)
    try:
        scope = await authorizer.accessible_folders(principal, "folder:read")
    except AuthorizerUnavailable as exc:
        raise _unavailable() from exc
    return IdentityMeOut(
        principal_id=resolved.grantee_id,
        principal_type=_principal_type(ref, resolved),
        idp="entra" if ref is not None and ref.kind == "entra" else "local",
        via=via,
        display_name=display_name,
        username=username,
        preferred_username=preferred_username,
        read_only=resolved.read_only,
        interactive=is_interactive(principal),
        groups=await _groups(session, members),
        assignments=await _assignments(session, members, scope.covers),
        global_permissions=sorted(await global_permissions_for(session, principal, resolved)),
    )


@router.patch(
    "",
    response_model=IdentityProfileOut,
    dependencies=[require("me:update")],
    responses={
        401: {"description": "Missing or invalid credentials"},
        403: {
            "description": "Restricted bootstrap session (password-rotation-required), or a PAT, "
            "key or other non-interactive credential (interactive-session-required)"
        },
        409: {
            "description": "The display name is managed by the identity provider: an Entra "
            "principal or a service account"
        },
        422: {"description": "Validation error (blank, too long, control characters, extra field)"},
    },
)
async def update_me(
    body: IdentityDisplayNameIn,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    uow: UnitOfWorkDep,
) -> IdentityProfileOut:
    """Change the caller's own display name (local users only); one audited write."""
    resolved = await _resolved(request, principal)
    ref = parse_principal_id(resolved.grantee_id)
    if ref is None or ref.kind != "local_user" or ref.local_id is None:
        detail = (
            "A service account is renamed through its own route."
            if ref is not None and ref.kind == "service_account"
            else "The display name of this account comes from its identity provider "
            "(Microsoft Entra ID) and cannot be changed here."
        )
        raise ProblemError(
            409, "display-name-managed-externally", "Display name managed externally", detail
        )
    user = await change_display_name(uow, ref.local_id, body.display_name)
    return IdentityProfileOut(
        principal_id=resolved.grantee_id,
        display_name=user.display_name,
        username=user.username,
        updated_at=user.updated_at,
    )


async def _readable_folder(
    request: Request, principal: Principal, session: AsyncSession, folder_id: uuid.UUID
) -> FolderFacts:
    """The folder's facts once the PDP allows ``folder:read``; otherwise the uniform 404.

    The check records every denial (AUD-04), and an unknown folder and an unreadable one are the
    same answer, so a probing caller learns nothing about which folders exist (T-8-01).
    """
    authorizer = request.app.state.authorizer_factory(request)
    try:
        decision = await authorizer.check(principal, "folder:read", folder_id)
    except AuthorizerUnavailable as exc:
        raise _unavailable() from exc
    if decision.outcome is not Outcome.ALLOW:
        raise _not_found()
    facts = decision.folder or await load_folder_facts(session, folder_id)
    if facts is None:
        raise _not_found()
    return facts


@router.get(
    "/capabilities",
    response_model=IdentityMeCapabilitiesOut,
    dependencies=[require("me:read")],
    responses={
        401: {"description": "Missing or invalid credentials"},
        403: {"description": "Restricted bootstrap session (password rotation required)"},
        404: {"description": "Folder not found or not readable by the caller"},
        422: {"description": "Invalid folder id"},
    },
)
async def get_capabilities(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    session: Annotated[AsyncSession, Depends(read_session)],
    folder_id: Annotated[uuid.UUID | None, Query()] = None,
) -> IdentityMeCapabilitiesOut:
    """What the caller may do at one folder, or globally without ``folder_id``."""
    resolved = await _resolved(request, principal)
    if folder_id is None:
        grantees = grantees_for(principal)
        root = await root_id(session)
        return IdentityMeCapabilitiesOut(
            folder_id=None,
            roles=sorted(await effective_roles(session, grantees, root)),
            permissions=sorted(await global_permissions_for(session, principal, resolved)),
            inheritance_blocked=False,
            grantable_roles=[],
        )
    facts = await _readable_folder(request, principal, session, folder_id)
    roles = await effective_roles(session, grantees_for(principal), facts.id)
    permissions = await folder_capabilities(session, principal, resolved, facts)
    cut = await inheritance_cut(session, facts.id)
    return IdentityMeCapabilitiesOut(
        folder_id=facts.id,
        roles=sorted(roles),
        permissions=sorted(permissions),
        inheritance_blocked=cut is not None,
        # Only a caller whose grant:manage survives narrowing here could grant at all.
        grantable_roles=grantable_roles(roles, facts.kind) if "grant:manage" in permissions else [],
    )


__all__ = [
    "IdentityMeAssignmentOut",
    "IdentityMeCapabilitiesOut",
    "IdentityMeGroupOut",
    "IdentityMeOut",
    "router",
]
