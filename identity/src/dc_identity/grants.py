"""Grant domain service: roles delegated to individuals at any folder (RBAC-03, RBAC-05, RBAC-09).

Decisions D-30 (level binding), D-32 (ceiling, also for revoking), D-33 (no self-grant) and D-34
(never remove the last Global admin). The ``require("grant:manage")`` gate runs before the handler
in its own session, so it is only a coarse gate; every binding check is repeated here, on the unit
of work's session, after taking the shared authorization-write advisory lock. A block-flag write,
another grant and a revocation therefore cannot interleave with a decision made on stale roles.

Grantees are local users (``local:<uuid>`` of an active user), Entra users (``entra:<tid>:<oid>``
of an active ``entra_users`` row), local groups (the bare lowercase group uuid), Entra groups (the
GUID of an active group registration) and Entra service principals (``entra:<tid>:<oid>`` of an
active service-principal registration, direct or just-in-time) and local service accounts
(``sa:<uuid>`` of an active account). A group, service principal or service account is grantable
only inside the Organization that owns it, or anywhere when ROOT owns it (RBAC-08, D-59, D-69,
D-77); the Graph confirmation of an Entra group GUID happens in the route before this module
writes (D-64).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from dc_auth.roles import Role, permissions_of
from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from sqlalchemy import select

from dc_identity.access import effective_roles, grantee_set, grantees_for
from dc_identity.authz import load_folder_facts
from dc_identity.directory import DirectoryUnavailable
from dc_identity.gate import directory_unavailable
from dc_identity.guards import (
    check_ceiling,
    check_level_binding,
    check_not_self,
    ensure_not_last_global_admin,
    lock_authz_writes,
    reject_archived,
)
from dc_identity.models import (
    EntraRegistration,
    EntraUser,
    LocalGroup,
    RoleAssignment,
    ServiceAccount,
    User,
)
from dc_identity.principal_ids import parse_principal_id
from dc_identity.sessions import root_folder_id
from dc_identity.users import normalize_username

if TYPE_CHECKING:
    from dc_auth.authorizer import FolderFacts
    from dc_auth.principal import Principal
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dc_identity.directory import DirectoryClient

LOCAL_PREFIX = "local:"


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


def _unknown_principal() -> ProblemError:
    # One fixed body for every unresolvable grantee form, so a caller learns nothing about which
    # ids exist or are inactive.
    return ProblemError(
        422,
        "unknown-principal",
        "Unprocessable Content",
        "The principal does not exist or cannot receive grants.",
    )


async def local_user_principal(session: AsyncSession, username: str) -> str:
    """``local:<uuid>`` of the active local user with exactly this username (normalized).

    Lets a grant name a local user by username, as adding a group member does, so an Org admin
    who may not list users (no global ``user:read``) can still grant to one. An unknown or
    inactive username is the same fixed ``unknown-principal`` body as an unknown id.
    """
    user_id = (
        await session.execute(
            select(User.id).where(
                User.is_active.is_(True), User.username == normalize_username(username)
            )
        )
    ).scalar_one_or_none()
    if user_id is None:
        raise _unknown_principal()
    return f"{LOCAL_PREFIX}{user_id}"


async def _require_active_user(session: AsyncSession, principal_id: str) -> None:
    """The grantee must be ``local:<uuid>`` or ``entra:<tid>:<oid>`` of an existing, active user.

    ``parse_principal_id`` accepts only the canonical lowercase spelling, so the stored
    ``principal_id`` always equals what the user's own token resolves to.
    """
    ref = parse_principal_id(principal_id)
    if ref is None:
        raise _unknown_principal()
    if ref.kind == "local_user":
        active = (
            await session.execute(
                select(User.id).where(User.id == ref.local_id, User.is_active.is_(True))
            )
        ).scalar_one_or_none()
    elif ref.kind == "entra":
        active = (
            await session.execute(
                select(EntraUser.id).where(
                    EntraUser.tenant_id == ref.tenant_id,
                    EntraUser.object_id == ref.object_id,
                    EntraUser.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
    else:
        active = None
    if active is None:
        raise _unknown_principal()


async def _require_grantable_group(
    session: AsyncSession,
    actor: Principal,
    facts: FolderFacts,
    principal_id: str,
    *,
    global_admin_anywhere: bool = False,
) -> None:
    """The grantee must be an existing group whose owner is ROOT or an ancestor of the folder.

    An unknown group and a group the actor cannot see are the same 404 (no existence leak). A
    group the actor can read but whose owner Organization does not contain the folder is a 422
    ``group-scope-mismatch``. ROOT is an ancestor of every folder, so "owned by ROOT" and "owned
    by an ancestor" are one membership test on ``facts.ancestor_ids``.
    """
    try:
        group_id = uuid.UUID(principal_id)
    except ValueError:
        raise _not_found() from None
    if principal_id != str(group_id):
        raise _not_found()
    owner_id = (
        await session.execute(select(LocalGroup.owner_folder_id).where(LocalGroup.id == group_id))
    ).scalar_one_or_none()
    if owner_id is None:
        raise _not_found()
    await _require_owner_scope(
        session, actor, facts, owner_id, "group:read", global_admin_anywhere=global_admin_anywhere
    )


async def _require_owner_scope(
    session: AsyncSession,
    actor: Principal,
    facts: FolderFacts,
    owner_id: uuid.UUID | None,
    read_permission: str,
    *,
    slug: str = "group-scope-mismatch",
    detail: str = "This principal cannot be granted at this folder.",
    global_admin_anywhere: bool = False,
) -> None:
    """The owner (ROOT or an Organization) must contain the folder; else 422 or 404 (D-59, D-77).

    With ``global_admin_anywhere`` (role grants only) a Global admin is not confined: they may
    grant an Organization-owned group, Entra group, service principal or service account on any
    folder (quick 261119, user decision). The owner still confines everyone else, so an Org
    admin's own groups stay inside their Organization, and member eligibility is unchanged.
    A caller who can read the registration at its owner learns that the folder is out of scope
    (422 ``group-scope-mismatch``, or ``slug``); everyone else gets the same 404 as for an
    unknown grantee.
    """
    if owner_id is not None and owner_id in facts.ancestor_ids:
        return
    if (
        global_admin_anywhere
        and owner_id is not None
        and Role.GLOBAL_ADMIN.value
        in await effective_roles(session, grantees_for(actor), await root_folder_id(session))
    ):
        return
    if owner_id is not None:
        held = permissions_of(await effective_roles(session, grantees_for(actor), owner_id))
        if read_permission in held:
            raise ProblemError(422, slug, "Unprocessable Content", detail)
    raise _not_found()


async def _registration_owner(
    session: AsyncSession, kind: str, tenant_id: str | None, object_id: str
) -> uuid.UUID:
    """Owner folder of the active registration of ``kind`` for the object, else the 404."""
    stmt = select(EntraRegistration.owner_folder_id).where(
        EntraRegistration.kind == kind,
        EntraRegistration.object_id == object_id,
        EntraRegistration.is_active.is_(True),
    )
    if tenant_id is not None:
        stmt = stmt.where(EntraRegistration.tenant_id == tenant_id)
    owner_id = (await session.execute(stmt)).scalar_one_or_none()
    if owner_id is None:
        raise _not_found()
    return owner_id


async def _require_grantable_entra_group(
    session: AsyncSession,
    actor: Principal,
    facts: FolderFacts,
    object_id: str,
    *,
    global_admin_anywhere: bool = False,
) -> None:
    """The grantee must be the GUID of an active group registration whose owner contains the folder.

    Group GUIDs are treated as globally unique across tenants, so the lookup is by GUID alone. An
    unregistered GUID is the same 404 as an unreadable one (no existence leak, D-64).
    """
    owner_id = await _registration_owner(session, "group", None, object_id)
    await _require_owner_scope(
        session, actor, facts, owner_id, "entra:read", global_admin_anywhere=global_admin_anywhere
    )


async def _require_grantable_service_principal(
    session: AsyncSession,
    actor: Principal,
    facts: FolderFacts,
    principal_id: str,
    *,
    global_admin_anywhere: bool = False,
) -> None:
    """The grantee must be ``entra:<tid>:<oid>`` of an active service-principal row (D-59, D-77).

    The row may be a direct registration or the just-in-time row of a group-admitted principal;
    either way it names an owner, and the same owner rule as for Entra groups applies.
    """
    ref = parse_principal_id(principal_id)
    if ref is None or ref.kind != "entra" or ref.object_id is None:
        raise _not_found()
    owner_id = await _registration_owner(session, "service_principal", ref.tenant_id, ref.object_id)
    await _require_owner_scope(
        session, actor, facts, owner_id, "entra:read", global_admin_anywhere=global_admin_anywhere
    )


async def _require_grantable_service_account(
    session: AsyncSession,
    actor: Principal,
    facts: FolderFacts,
    principal_id: str,
    *,
    global_admin_anywhere: bool = False,
) -> None:
    """The grantee must be ``sa:<uuid>`` of an active account whose owner contains the folder.

    ROOT-owned accounts are grantable anywhere; an Organization-owned account only inside its
    Organization (D-69). An unknown or inactive account is the fixed ``unknown-principal``; a
    caller who can read the account learns of an out-of-scope folder (422
    ``principal-scope-mismatch``), everyone else gets the 404.
    """
    ref = parse_principal_id(principal_id)
    if ref is None or ref.kind != "service_account":
        raise _unknown_principal()
    owner_id = (
        await session.execute(
            select(ServiceAccount.owner_folder_id).where(
                ServiceAccount.id == ref.service_account_id,
                ServiceAccount.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if owner_id is None:
        raise _unknown_principal()
    await _require_owner_scope(
        session,
        actor,
        facts,
        owner_id,
        "service-account:read",
        slug="principal-scope-mismatch",
        detail="This principal cannot be used outside its Organization.",
        global_admin_anywhere=global_admin_anywhere,
    )


async def confirm_entra_group(
    session_factory: async_sessionmaker[AsyncSession],
    directory: DirectoryClient,
    object_id: str,
) -> None:
    """Graph confirms the GUID in its registration's tenant before a link or write opens (D-64).

    No active registration means no Graph call at all: the caller's eligibility check answers
    that. Graph not finding the group is the fixed ``unknown-principal`` (422); Graph trouble is
    the fail-closed 503, never a pass.
    """
    # Deferred: dc_identity.entra imports this module for ``_not_found``.
    from dc_identity.entra import registration_tenant  # noqa: PLC0415

    async with session_factory() as session:
        tenant = await registration_tenant(session, "group", object_id)
    if tenant is None:
        return
    try:
        found = await directory.group(tenant, object_id)
    except DirectoryUnavailable as exc:
        raise directory_unavailable() from exc
    if found is None:
        raise _unknown_principal()


async def member_is_eligible(
    session: AsyncSession,
    actor: Principal,
    facts: FolderFacts,
    member_type: str,
    member_id: str,
) -> bool:
    """May this group or service account be linked at the folder? The grant rules, as a boolean.

    Only local groups, Entra group registrations and service accounts qualify (A3). Every
    refusal (unknown, inactive, malformed, owned outside the folder's path) collapses to False so
    the answer reveals nothing about which ids exist; any other failure is not an answer and
    propagates (a 503 stays an outage).
    """
    try:
        if member_type == "local_group":
            await _require_grantable_group(session, actor, facts, member_id)
        elif member_type == "entra_group":
            await _require_grantable_entra_group(session, actor, facts, member_id)
        elif member_type == "service_account":
            await _require_grantable_service_account(session, actor, facts, member_id)
        else:
            return False
    except ProblemError as exc:
        if exc.status in (404, 422):
            return False
        raise
    return True


async def create_grant(
    uow: UnitOfWork,
    actor: Principal,
    folder_id: uuid.UUID,
    *,
    principal_type: str,
    principal_id: str,
    role: str,
) -> RoleAssignment:
    """Grant ``role`` to a user, group or service principal at a folder; one ``grant.created``."""
    session = uow.session
    await lock_authz_writes(session)
    facts = await load_folder_facts(session, folder_id)
    if facts is None:
        raise _not_found()
    reject_archived(facts)
    check_level_binding(role, facts.kind)
    grantees = grantees_for(actor)
    check_ceiling(await effective_roles(session, grantees, folder_id), role)
    check_not_self(await grantee_set(session, grantees), principal_type, principal_id)
    if principal_type == "local_group":
        await _require_grantable_group(
            session, actor, facts, principal_id, global_admin_anywhere=True
        )
    elif principal_type == "user":
        await _require_active_user(session, principal_id)
    elif principal_type == "entra_group":
        await _require_grantable_entra_group(
            session, actor, facts, principal_id, global_admin_anywhere=True
        )
    elif principal_type == "service_principal":
        await _require_grantable_service_principal(
            session, actor, facts, principal_id, global_admin_anywhere=True
        )
    elif principal_type == "service_account":
        await _require_grantable_service_account(
            session, actor, facts, principal_id, global_admin_anywhere=True
        )
    else:
        raise _unknown_principal()

    # Existence check under the lock, not an IntegrityError mapped by message (pitfall 9).
    existing = (
        await session.execute(
            select(RoleAssignment.id).where(
                RoleAssignment.folder_id == folder_id,
                RoleAssignment.principal_type == principal_type,
                RoleAssignment.principal_id == principal_id,
                RoleAssignment.role == role,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ProblemError(409, "conflict", "Conflict", "This grant already exists.")

    grant = RoleAssignment(
        folder_id=folder_id, principal_type=principal_type, principal_id=principal_id, role=role
    )
    session.add(grant)
    await session.flush()
    await session.refresh(grant)
    uow.audit(
        "grant.created",
        "grant",
        grant.id,
        before=None,
        after=UnitOfWork.snapshot(grant),
        scope_path=facts.ancestor_ids,
    )
    await uow.commit()
    return grant


async def revoke_grant(uow: UnitOfWork, actor: Principal, grant_id: uuid.UUID) -> None:
    """Revoke one grant; the same ceiling as granting applies (D-32). One ``grant.revoked``.

    Revocation is allowed inside archived subtrees (OQ3) and by the grantee themselves (D-33
    restricts granting only); the last-Global-admin rule still holds.
    """
    session = uow.session
    await lock_authz_writes(session)
    grant = (
        await session.execute(
            select(RoleAssignment).where(RoleAssignment.id == grant_id).with_for_update()
        )
    ).scalar_one_or_none()
    if grant is None:
        raise _not_found()
    facts = await load_folder_facts(session, grant.folder_id)
    if facts is None:  # pragma: no cover - the foreign key guarantees the folder
        raise _not_found()
    check_ceiling(await effective_roles(session, grantees_for(actor), grant.folder_id), grant.role)
    await ensure_not_last_global_admin(session, grant)
    before = UnitOfWork.snapshot(grant)
    await session.delete(grant)
    uow.audit(
        "grant.revoked",
        "grant",
        grant_id,
        before=before,
        after=None,
        scope_path=facts.ancestor_ids,
    )
    await uow.commit()


async def grant_folder(grant_id: uuid.UUID, session: AsyncSession) -> uuid.UUID:
    """Route target resolver: the folder a grant sits on; unknown is the same 404 as unreadable."""
    folder_id = (
        await session.execute(select(RoleAssignment.folder_id).where(RoleAssignment.id == grant_id))
    ).scalar_one_or_none()
    if folder_id is None:
        raise _not_found()
    return folder_id


__all__ = ["LOCAL_PREFIX", "create_grant", "grant_folder", "local_user_principal", "revoke_grant"]
