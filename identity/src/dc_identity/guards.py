"""Guards shared by every identity write that changes who may do what (D-29, T-02-13).

Authorization-mutating writes (block flags, grants, revokes and, in later plans, group
memberships) serialise on one transaction-scoped advisory lock. Without it a block-flag write could
interleave with a grant write and leave a decision based on a state that never existed. The lock
is released automatically at commit or rollback, so no code path can leak it.

The escalation guards of RBAC-05 live here too: level binding (D-30), the grant ceiling (D-32),
no self-grant (D-33) and never removing the last Global admin (D-34). Each raises a
``ProblemError`` with a fixed detail that never echoes submitted input. They are only meaningful
when called after :func:`lock_authz_writes` and on the unit of work's own session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dc_auth.roles import Role, can_grant, is_bound_to, permissions_of
from dc_core.problems import ProblemError
from sqlalchemy import Text, cast, func, select, text

from dc_identity.access import effective_roles, grantees_for
from dc_identity.models import Folder, RoleAssignment, User

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterable

    from dc_auth.authorizer import FolderFacts
    from dc_auth.principal import Principal
    from sqlalchemy.ext.asyncio import AsyncSession

AUTHZ_WRITE_LOCK = "dc-identity-authz-write"

_LOCK = text("SELECT pg_advisory_xact_lock(hashtext(:key))")


async def lock_authz_writes(session: AsyncSession) -> None:
    """Take the authorization-write lock for the rest of the current transaction.

    Call this as the first statement of every write that changes grants, group memberships or
    block-inheritance flags. The key is a bound parameter.
    """
    await session.execute(_LOCK, {"key": AUTHZ_WRITE_LOCK})


async def require_permission(
    session: AsyncSession, actor: Principal, folder_id: uuid.UUID, permission: str
) -> None:
    """Re-check ``permission`` at ``folder_id`` inside the write transaction, under the lock.

    The ``require()`` gate ran in its own session before the handler, so a revocation that
    committed in between would otherwise go unnoticed. Call after :func:`lock_authz_writes`: the
    lock makes this read see every earlier authorization write. A denial is a fixed 403 (the
    caller was allowed a moment ago, so the target is no secret).
    """
    held = permissions_of(await effective_roles(session, grantees_for(actor), folder_id))
    if permission not in held:
        raise ProblemError(
            403, "forbidden", "Forbidden", "You are not permitted to perform this action."
        )


def check_level_binding(role: str, kind: str) -> None:
    """D-30: a role is grantable only at the folder kinds it is bound to."""
    if not is_bound_to(role, kind):
        raise ProblemError(
            422,
            "role-level-mismatch",
            "Unprocessable Content",
            "This role cannot be granted at this kind of folder.",
        )


def check_ceiling(actor_roles: frozenset[str], role: str) -> None:
    """D-32: the actor may only grant or revoke roles under the ceiling of a role they hold here."""
    if not can_grant(actor_roles, role):
        raise ProblemError(
            403,
            "grant-ceiling-exceeded",
            "Forbidden",
            "You may not grant or revoke this role at this folder.",
        )


def grantable_roles(actor_roles: Iterable[str], kind: str) -> list[str]:
    """The roles the actor could grant at a folder of ``kind``: what ``/me/capabilities`` offers.

    Built on the same two predicates as the grant write's :func:`check_ceiling` (D-32) and
    :func:`check_level_binding` (D-30), so a role listed here passes both checks. The write
    still re-checks under the authorization lock.
    """
    held = frozenset(actor_roles)
    return sorted(
        role.value for role in Role if can_grant(held, role.value) and is_bound_to(role.value, kind)
    )


def check_not_self(
    actor_grantees: frozenset[tuple[str, str]], principal_type: str, principal_id: str
) -> None:
    """D-33: nobody grants a role to themselves (or to a group they belong to)."""
    if (principal_type, principal_id) in actor_grantees:
        raise ProblemError(
            403,
            "self-grant-forbidden",
            "Forbidden",
            "You may not grant a role to yourself.",
        )


async def check_group_change(session: AsyncSession, actor: Principal, group_id: uuid.UUID) -> None:
    """D-33 and A3: a membership change counts as granting every role the group holds.

    Adding a member gives them, removing one takes from them and deleting the group takes from
    all of them, every ``(role, folder)`` grant the group carries. The actor therefore needs the
    D-32 ceiling for each of those grants at its folder, evaluated with block flags applied; the
    first one they cannot grant is a 403. A group holding no grant is free to change. Call under
    :func:`lock_authz_writes`, so a grant to the group cannot slip in between check and write.
    """
    grantees = grantees_for(actor)
    rows = (
        await session.execute(
            select(RoleAssignment.folder_id, RoleAssignment.role)
            .where(
                RoleAssignment.principal_type == "local_group",
                RoleAssignment.principal_id == str(group_id),
            )
            .order_by(RoleAssignment.id)
        )
    ).all()
    held_at: dict[uuid.UUID, frozenset[str]] = {}
    for folder_id, role in rows:
        if folder_id not in held_at:
            held_at[folder_id] = await effective_roles(session, grantees, folder_id)
        check_ceiling(held_at[folder_id], role)


def reject_archived(facts: FolderFacts) -> None:
    """OQ3: nothing new may be granted inside an archived subtree (revocation stays allowed)."""
    if facts.archived:
        raise ProblemError(
            409,
            "folder-archived",
            "Conflict",
            "The folder is archived; its subtree is read-only.",
        )


async def ensure_not_last_global_admin(session: AsyncSession, grant: RoleAssignment) -> None:
    """D-34: refuse to remove the last direct ``global_admin`` grant of an active local user.

    Applies only to a direct user ``global_admin`` grant. Counts active local users holding such
    a grant on the ROOT folder other than ``grant`` itself; group-based and non-local Global
    admins do not count. Call under :func:`lock_authz_writes`, or two racing revocations each see
    the other's grant and both succeed.
    """
    if grant.role != Role.GLOBAL_ADMIN.value or grant.principal_type != "user":
        return
    remaining = (
        await session.execute(
            select(func.count())
            .select_from(RoleAssignment)
            .join(Folder, Folder.id == RoleAssignment.folder_id)
            .join(User, RoleAssignment.principal_id == func.concat("local:", cast(User.id, Text)))
            .where(
                Folder.kind == "root",
                RoleAssignment.principal_type == "user",
                RoleAssignment.role == Role.GLOBAL_ADMIN.value,
                RoleAssignment.id != grant.id,
                User.is_active.is_(True),
            )
        )
    ).scalar_one()
    if remaining == 0:
        raise ProblemError(
            409,
            "last-global-admin",
            "Conflict",
            "The last Global admin cannot be removed.",
        )


__all__ = [
    "AUTHZ_WRITE_LOCK",
    "check_ceiling",
    "check_group_change",
    "check_level_binding",
    "check_not_self",
    "ensure_not_last_global_admin",
    "grantable_roles",
    "lock_authz_writes",
    "reject_archived",
]
