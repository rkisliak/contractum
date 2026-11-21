"""Local group domain service: owned groups and their members (RBAC-02, RBAC-08, D-27, D-33).

A group is owned by ROOT or an Organization folder (D-27). Group permissions are evaluated at the
owner folder by the ordinary ``require()`` gate, so an Organization admin manages exactly their
own Organization's groups. Membership is an authorization write: a member gains every role the
group holds, so adding, removing and deleting go through the same D-32 ceiling as granting
(:func:`dc_identity.guards.check_group_change`, D-33 and A3), and nobody adds themselves.

Every authorization-mutating function takes :func:`lock_authz_writes` first and re-runs its guards
on the unit of work's session; the gate before the handler is only a coarse pre-check. Each write
records exactly one audit event.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from dc_identity.authz import load_folder_facts
from dc_identity.grants import (
    LOCAL_PREFIX,
    _not_found,
    _require_owner_scope,
    _unknown_principal,
)
from dc_identity.guards import (
    check_group_change,
    lock_authz_writes,
    reject_archived,
    require_permission,
)
from dc_identity.models import (
    EntraUser,
    GroupMember,
    LocalGroup,
    RoleAssignment,
    ServiceAccount,
    User,
)
from dc_identity.principal_ids import parse_principal_id
from dc_identity.users import normalize_username

if TYPE_CHECKING:
    from dc_auth.authorizer import FolderFacts
    from dc_auth.principal import Principal
    from sqlalchemy.ext.asyncio import AsyncSession

_DUPLICATE_NAME = "uq_local_groups_owner_lower_name"


def _conflict(detail: str) -> ProblemError:
    return ProblemError(409, "conflict", "Conflict", detail)


async def group_owner(group_id: uuid.UUID, session: AsyncSession) -> uuid.UUID:
    """Route target resolver: the owner folder of a group; unknown is the same 404 as unreadable."""
    owner_id = (
        await session.execute(select(LocalGroup.owner_folder_id).where(LocalGroup.id == group_id))
    ).scalar_one_or_none()
    if owner_id is None:
        raise _not_found()
    return owner_id


async def _require_manage(session: AsyncSession, actor: Principal, owner_id: uuid.UUID) -> None:
    """Re-check ``group:manage`` at the owner inside the write transaction, under the lock.

    The gate ran in its own session before the handler, so a revocation that committed in between
    would otherwise go unnoticed by a group that currently holds no grant.
    """
    await require_permission(session, actor, owner_id, "group:manage")


async def create_group(
    uow: UnitOfWork, actor: Principal, owner: FolderFacts, *, name: str
) -> LocalGroup:
    """Create a group owned by ROOT or an Organization; one ``group.created`` event.

    Takes the authorization-write lock and re-checks ``group:create`` at the owner under it, so a
    revocation that committed after the gate cannot be outrun.
    """
    if owner.kind not in ("root", "organization"):
        raise ProblemError(
            422,
            "group-owner-not-allowed",
            "Unprocessable Content",
            "A group can only be owned by ROOT or an Organization.",
        )
    session = uow.session
    await lock_authz_writes(session)
    await require_permission(session, actor, owner.id, "group:create")
    reject_archived(owner)
    group = LocalGroup(owner_folder_id=owner.id, name=name)
    # No SAVEPOINT (see create_folder): the unique index is the arbiter, and the 409 makes the
    # unit of work roll back, so two concurrent creates of one name yield exactly one 201.
    session.add(group)
    try:
        await session.flush()
    except IntegrityError as exc:
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == _DUPLICATE_NAME:
            raise _conflict("A group with this name already exists here.") from exc
        raise
    await session.refresh(group)
    uow.audit(
        "group.created",
        "group",
        group.id,
        before=None,
        after=UnitOfWork.snapshot(group),
        scope_path=owner.ancestor_ids,
    )
    await uow.commit()
    return group


async def _resolve_member(
    session: AsyncSession, *, username: str | None, principal_id: str | None
) -> tuple[str, str]:
    """The one active member named by exact username or exact canonical principal id (OQ2).

    Returns ``(member_type, member_id)``: ``("user", "local:<uuid>")`` for an active local user,
    ``("user", "entra:<tid>:<oid>")`` for an active Entra user record (D-80) and
    ``("service_account", "sa:<uuid>")`` for an active service account. Every form that does not
    resolve (unknown, inactive, malformed, other spelling of a uuid, any other namespace) is the
    same fixed ``unknown-principal`` body.
    """
    if (username is None) == (principal_id is None):
        raise ProblemError(
            422,
            "validation-error",
            "Unprocessable Content",
            "The request failed validation.",
            errors=[{"pointer": "/", "detail": "give exactly one of username and principal_id"}],
        )
    if username is not None:
        user_id = (
            await session.execute(
                select(User.id).where(
                    User.is_active.is_(True), User.username == normalize_username(username)
                )
            )
        ).scalar_one_or_none()
        if user_id is None:
            raise _unknown_principal()
        return "user", f"{LOCAL_PREFIX}{user_id}"
    assert principal_id is not None  # noqa: S101 - narrowed by the check above
    ref = parse_principal_id(principal_id)
    if ref is None:
        raise _unknown_principal()
    if ref.kind == "local_user":
        found = (
            await session.execute(
                select(User.id).where(User.id == ref.local_id, User.is_active.is_(True))
            )
        ).scalar_one_or_none()
        if found is None:
            raise _unknown_principal()
        return "user", ref.canonical
    if ref.kind == "entra":
        found = (
            await session.execute(
                select(EntraUser.id).where(
                    EntraUser.tenant_id == ref.tenant_id,
                    EntraUser.object_id == ref.object_id,
                    EntraUser.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if found is None:
            raise _unknown_principal()
        return "user", ref.canonical
    if ref.kind == "service_account":
        found = (
            await session.execute(
                select(ServiceAccount.id).where(
                    ServiceAccount.id == ref.service_account_id,
                    ServiceAccount.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if found is None:
            raise _unknown_principal()
        return "service_account", ref.canonical
    raise _unknown_principal()


async def _require_service_account_scope(
    session: AsyncSession, actor: Principal, group_facts: FolderFacts, member_id: str
) -> None:
    """An Organization-owned account joins only groups of its own Organization (D-69).

    A ROOT-owned account joins any group. A caller who can read the account at its owner learns
    the mismatch (422 ``principal-scope-mismatch``); everyone else gets the same 404 as for an
    unknown account. The group's owner folder is the folder whose ancestors must contain the
    account's owner, so a ROOT-owned group is out of scope for an Organization-owned account.
    """
    ref = parse_principal_id(member_id)
    assert ref is not None  # noqa: S101 - resolved by _resolve_member
    owner_id = (
        await session.execute(
            select(ServiceAccount.owner_folder_id).where(
                ServiceAccount.id == ref.service_account_id
            )
        )
    ).scalar_one_or_none()
    await _require_owner_scope(
        session,
        actor,
        group_facts,
        owner_id,
        "service-account:read",
        slug="principal-scope-mismatch",
        detail="This principal cannot be used outside its Organization.",
    )


async def add_member(
    uow: UnitOfWork,
    actor: Principal,
    group_id: uuid.UUID,
    *,
    username: str | None,
    principal_id: str | None,
) -> GroupMember:
    """Add a user or service account to a group; counts as granting every role it holds (D-33).

    One ``group.member_added`` event. Refused for the actor themselves, for an archived owner
    Organization (409) and beyond the actor's ceiling on any of the group's grants (403).
    """
    session = uow.session
    await lock_authz_writes(session)
    owner_id = (
        await session.execute(select(LocalGroup.owner_folder_id).where(LocalGroup.id == group_id))
    ).scalar_one_or_none()
    if owner_id is None:
        raise _not_found()
    await _require_manage(session, actor, owner_id)
    facts = await load_folder_facts(session, owner_id)
    if facts is None:  # pragma: no cover - the foreign key guarantees the folder
        raise _not_found()
    reject_archived(facts)
    member_type, member_id = await _resolve_member(
        session, username=username, principal_id=principal_id
    )
    if member_type == "service_account":
        await _require_service_account_scope(session, actor, facts, member_id)
    if member_id == actor.id:
        raise ProblemError(
            403, "self-grant-forbidden", "Forbidden", "You may not grant a role to yourself."
        )
    await check_group_change(session, actor, group_id)
    existing = (
        await session.execute(
            select(GroupMember.group_id).where(
                GroupMember.group_id == group_id, GroupMember.member_id == member_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise _conflict("This principal is already a member of the group.")
    member = GroupMember(group_id=group_id, member_type=member_type, member_id=member_id)
    session.add(member)
    await session.flush()
    await session.refresh(member)
    uow.audit(
        "group.member_added",
        "group",
        group_id,
        before=None,
        after=UnitOfWork.snapshot(member),
        scope_path=facts.ancestor_ids,
    )
    await uow.commit()
    return member


async def rename_group(
    uow: UnitOfWork, actor: Principal, group_id: uuid.UUID, *, name: str
) -> LocalGroup:
    """Rename a group; a no-op (no event) when unchanged. One ``group.renamed`` otherwise."""
    session = uow.session
    group = (
        await session.execute(select(LocalGroup).where(LocalGroup.id == group_id).with_for_update())
    ).scalar_one_or_none()
    if group is None:
        raise _not_found()
    await _require_manage(session, actor, group.owner_folder_id)
    facts = await load_folder_facts(session, group.owner_folder_id)
    if facts is None:  # pragma: no cover - the foreign key guarantees the folder
        raise _not_found()
    reject_archived(facts)
    if group.name == name:
        return group
    before = UnitOfWork.snapshot(group)
    group.name = name
    try:
        await session.flush()
    except IntegrityError as exc:
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == _DUPLICATE_NAME:
            raise _conflict("A group with this name already exists here.") from exc
        raise
    await session.refresh(group)
    uow.audit(
        "group.renamed",
        "group",
        group.id,
        before=before,
        after=UnitOfWork.snapshot(group),
        scope_path=facts.ancestor_ids,
    )
    await uow.commit()
    return group


async def remove_member(
    uow: UnitOfWork, actor: Principal, group_id: uuid.UUID, member_id: str
) -> None:
    """Remove a member; the ceiling applies as for adding (A3). One ``group.member_removed``.

    Allowed inside an archived Organization (OQ3), as revocation is.
    """
    session = uow.session
    await lock_authz_writes(session)
    owner_id = (
        await session.execute(select(LocalGroup.owner_folder_id).where(LocalGroup.id == group_id))
    ).scalar_one_or_none()
    if owner_id is None:
        raise _not_found()
    await _require_manage(session, actor, owner_id)
    facts = await load_folder_facts(session, owner_id)
    if facts is None:  # pragma: no cover - the foreign key guarantees the folder
        raise _not_found()
    member = (
        await session.execute(
            select(GroupMember)
            .where(GroupMember.group_id == group_id, GroupMember.member_id == member_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if member is None:
        raise _not_found()
    await check_group_change(session, actor, group_id)
    before = UnitOfWork.snapshot(member)
    await session.delete(member)
    uow.audit(
        "group.member_removed",
        "group",
        group_id,
        before=before,
        after=None,
        scope_path=facts.ancestor_ids,
    )
    await uow.commit()


REMOVED_GRANT_IDS_LIMIT = 20


async def delete_group(uow: UnitOfWork, actor: Principal, group_id: uuid.UUID) -> None:
    """Delete a group with its members and role assignments; one bounded ``group.deleted``.

    The ceiling applies as for removing every member (A3). The event's ``before`` carries the
    group snapshot, ``removed_grants`` and ``removed_members`` counts and at most
    ``REMOVED_GRANT_IDS_LIMIT`` ``removed_grant_ids`` (pitfall 8: events stay small).
    """
    session = uow.session
    await lock_authz_writes(session)
    group = (
        await session.execute(select(LocalGroup).where(LocalGroup.id == group_id).with_for_update())
    ).scalar_one_or_none()
    if group is None:
        raise _not_found()
    await _require_manage(session, actor, group.owner_folder_id)
    facts = await load_folder_facts(session, group.owner_folder_id)
    if facts is None:  # pragma: no cover - the foreign key guarantees the folder
        raise _not_found()
    await check_group_change(session, actor, group_id)

    grants = (RoleAssignment.principal_type == "local_group") & (
        RoleAssignment.principal_id == str(group_id)
    )
    removed_grants = (
        await session.execute(select(func.count()).select_from(RoleAssignment).where(grants))
    ).scalar_one()
    removed_ids = [
        str(grant_id)
        for grant_id in (
            await session.execute(
                select(RoleAssignment.id)
                .where(grants)
                .order_by(RoleAssignment.id)
                .limit(REMOVED_GRANT_IDS_LIMIT)
            )
        ).scalars()
    ]
    removed_members = (
        await session.execute(
            select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group_id)
        )
    ).scalar_one()
    before = {
        **UnitOfWork.snapshot(group),
        "removed_grants": removed_grants,
        "removed_grant_ids": removed_ids,
        "removed_members": removed_members,
    }
    await session.execute(delete(RoleAssignment).where(grants))
    # The group's members go with it: group_members.group_id is ON DELETE CASCADE.
    await session.delete(group)
    uow.audit(
        "group.deleted",
        "group",
        group_id,
        before=before,
        after=None,
        scope_path=facts.ancestor_ids,
    )
    await uow.commit()


__all__ = [
    "REMOVED_GRANT_IDS_LIMIT",
    "add_member",
    "create_group",
    "delete_group",
    "group_owner",
    "remove_member",
    "rename_group",
]
