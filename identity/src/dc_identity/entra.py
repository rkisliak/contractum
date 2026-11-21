"""Entra registrations and just-in-time records (D-57, D-58, D-59, D-73, D-75, D-76, D-81).

Registration (Plan 05): ``register_group``, ``unregister_group``, ``register_service_principal``
and ``register_user`` are the audited writes behind ``/api/v1/identity/entra/...``. Each takes the
authorization-write lock first, re-checks ``entra:manage`` (or ``user:create``) in its own
transaction, and writes exactly one event. The route confirms the GUID with the directory before
opening the write (D-64), so ``display_name`` arrives already confirmed; registration grants
nothing (D-57).

Just-in-time records (Plan 04):

The PrincipalGate decides admission in a read session; the two writers here run afterwards, each in
its own ``UnitOfWork`` (actor = the principal), so a PDP read path never writes inside its own
session. Both take a transaction-scoped advisory lock per ``(tenant_id, object_id)`` and re-read
under it, so N concurrent first requests produce one row and one audit event; the unique
constraints are the backstop. An event is written only when a row was created or changed.

``ensure_entra_user`` keeps the user's record (never deleted: grants, PATs and audit refer to it).
``ensure_entra_service_principal`` gives a service principal admitted only through a registered
group a ``directly_registered = false`` row that its owner can deactivate; that row never admits on
its own (the gate counts only directly registered rows and groups).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from dc_auth.principal import principal_to_actor
from dc_auth.roles import roles_with
from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from sqlalchemy import delete, exists, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from dc_identity.access import effective_roles, grantees_for, visible_folders_subquery
from dc_identity.authz import load_folder_facts
from dc_identity.grants import _not_found
from dc_identity.groups import REMOVED_GRANT_IDS_LIMIT
from dc_identity.guards import (
    check_ceiling,
    lock_authz_writes,
    reject_archived,
    require_permission,
)
from dc_identity.models import (
    EntraRegistration,
    EntraUser,
    Folder,
    OutboxEvent,
    RoleAssignment,
)
from dc_identity.sessions import root_folder_id

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dc_auth.authorizer import FolderFacts
    from dc_auth.principal import Principal
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_LOCK = text("SELECT pg_advisory_xact_lock(hashtext(:key))")
_DISPLAY_MAX = 200
_HINT_MAX = 256
_REGISTRATION_NAME_MAX = 256


async def _ancestor_ids(session: AsyncSession, folder_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
    target = aliased(Folder)
    ancestor = aliased(Folder)
    rows = await session.execute(
        select(ancestor.id)
        .join(target, ancestor.path.op("@>")(target.path))
        .where(target.id == folder_id)
        .order_by(func.nlevel(ancestor.path))
    )
    return tuple(rows.scalars())


async def ensure_entra_user(
    session_factory: async_sessionmaker[AsyncSession],
    principal: Principal,
    *,
    tenant_id: str,
    object_id: str,
    service: str,
) -> None:
    """Create the user's record on its first admitted request; refresh display data from tokens.

    A token without ``preferred_username`` never clears a stored hint. The hint is display-only
    and never audited (only ``preferred_username_set`` is).
    """
    display = (principal.display or object_id)[:_DISPLAY_MAX]
    hint = principal.username_hint[:_HINT_MAX] if principal.username_hint else None
    async with UnitOfWork(
        session_factory,
        actor=principal_to_actor(principal),
        outbox_model=OutboxEvent,
        service=service,
    ) as uow:
        session = uow.session
        await session.execute(_LOCK, {"key": f"entra-user:{tenant_id}:{object_id}"})
        row = (
            await session.execute(
                select(EntraUser)
                .where(EntraUser.tenant_id == tenant_id, EntraUser.object_id == object_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            row = EntraUser(
                tenant_id=tenant_id,
                object_id=object_id,
                display_name=display,
                preferred_username=hint,
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                return  # created by a concurrent first request: nothing left to do
            uow.audit(
                "entra_user.created",
                "entra_user",
                row.id,
                before=None,
                after=UnitOfWork.snapshot(row),
                scope_path=[await root_folder_id(session)],
            )
            await uow.commit()
            return
        if principal.via != "jwt":
            return  # only a token carries display data
        new_hint = hint if hint is not None else row.preferred_username
        if row.display_name == display and row.preferred_username == new_hint:
            return
        before = UnitOfWork.snapshot(row)
        row.display_name = display
        row.preferred_username = new_hint
        uow.audit(
            "entra_user.updated",
            "entra_user",
            row.id,
            before=before,
            after=UnitOfWork.snapshot(row),
            scope_path=[await root_folder_id(session)],
        )
        await uow.commit()


async def ensure_entra_service_principal(
    session_factory: async_sessionmaker[AsyncSession],
    principal: Principal,
    *,
    tenant_id: str,
    object_id: str,
    owner_folder_id: uuid.UUID,
    display_name: str,
    service: str,
) -> None:
    """Give a group-admitted service principal its owned, deactivatable row (once).

    ``owner_folder_id`` is the owner of its oldest admitting group registration (D-76). An
    existing row, direct or just-in-time, is never changed here.
    """
    name = (display_name or principal.display or object_id)[:_REGISTRATION_NAME_MAX]
    async with UnitOfWork(
        session_factory,
        actor=principal_to_actor(principal),
        outbox_model=OutboxEvent,
        service=service,
    ) as uow:
        session = uow.session
        await session.execute(_LOCK, {"key": f"entra-sp:{tenant_id}:{object_id}"})
        existing = await session.scalar(
            select(EntraRegistration.id).where(
                EntraRegistration.kind == "service_principal",
                EntraRegistration.tenant_id == tenant_id,
                EntraRegistration.object_id == object_id,
            )
        )
        if existing is not None:
            return
        row = EntraRegistration(
            kind="service_principal",
            tenant_id=tenant_id,
            object_id=object_id,
            owner_folder_id=owner_folder_id,
            display_name=name,
            directly_registered=False,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:
            return
        uow.audit(
            "entra_registration.created",
            "entra_registration",
            row.id,
            before=None,
            after=UnitOfWork.snapshot(row),
            scope_path=await _ancestor_ids(session, owner_folder_id),
        )
        await uow.commit()


# --- Registration (Plan 05, D-59, D-64) ---------------------------------------------------------

_NAME_MAX = 256
_OWNER_KINDS = ("root", "organization")


def resolve_tenant(tenant_ids: tuple[str, ...], requested: str | None) -> str:
    """The Entra tenant a registration is made in (AUTH-03, T-04-46).

    Only the tenants of configured Entra issuers are accepted. An omitted tenant is the only
    configured one; with several configured the caller must say which.
    """
    if not tenant_ids:
        raise ProblemError(
            503, "service-unavailable", "Service Unavailable", "Entra is not configured."
        )
    if requested is None:
        if len(tenant_ids) == 1:
            return tenant_ids[0]
        raise ProblemError(
            422,
            "validation-error",
            "Unprocessable Content",
            "tenant_id is required when several Entra tenants are configured.",
        )
    if requested not in tenant_ids:
        raise ProblemError(
            422, "validation-error", "Unprocessable Content", "Unknown Entra tenant."
        )
    return requested


def check_owner_kind(owner: FolderFacts) -> None:
    """D-59: only ROOT or an Organization owns a registration (as for local groups, D-27)."""
    if owner.kind not in _OWNER_KINDS:
        raise ProblemError(
            422,
            "owner-not-allowed",
            "Unprocessable Content",
            "Only ROOT or an Organization can own this.",
        )


def _conflict(detail: str) -> ProblemError:
    return ProblemError(409, "conflict", "Conflict", detail)


def _display(name: str, object_id: str, limit: int = _NAME_MAX) -> str:
    return (name or object_id)[:limit]


async def registration_tenant(session: AsyncSession, kind: str, object_id: str) -> str | None:
    """Tenant of the active registration of ``kind`` for ``object_id``, or ``None`` (D-64).

    The grant route asks Graph in this tenant, so a grant to an Entra group is confirmed where the
    group is registered, never in a tenant the caller chose. Object ids are GUIDs and treated as
    globally unique, so a second tenant holding the same GUID is not a case to resolve here; the
    oldest active registration answers.
    """
    return (
        await session.execute(
            select(EntraRegistration.tenant_id)
            .where(
                EntraRegistration.kind == kind,
                EntraRegistration.object_id == object_id,
                EntraRegistration.is_active.is_(True),
            )
            .order_by(EntraRegistration.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def entra_registration_owner(
    registration_id: uuid.UUID, session: AsyncSession, *, kind: str
) -> uuid.UUID:
    """Route target resolver: the owner folder of a registration of ``kind``.

    An unknown id and a registration of another kind are the same 404 as an unreadable owner.
    """
    owner_id = (
        await session.execute(
            select(EntraRegistration.owner_folder_id).where(
                EntraRegistration.id == registration_id, EntraRegistration.kind == kind
            )
        )
    ).scalar_one_or_none()
    if owner_id is None:
        raise _not_found()
    return owner_id


async def _owner_for_write(
    session: AsyncSession, actor: Principal, owner_folder_id: uuid.UUID
) -> FolderFacts:
    """Owner facts after the in-transaction ``entra:manage`` re-check (call under the lock)."""
    owner = await load_folder_facts(session, owner_folder_id)
    if owner is None:
        raise _not_found()
    check_owner_kind(owner)
    await require_permission(session, actor, owner.id, "entra:manage")
    reject_archived(owner)
    return owner


async def register_group(
    uow: UnitOfWork,
    actor: Principal,
    *,
    tenant_id: str,
    owner_folder_id: uuid.UUID,
    object_id: str,
    display_name: str,
) -> EntraRegistration:
    """Register an Entra group owned by ROOT or an Organization; one ``entra_registration.created``.

    ``display_name`` is the directory's confirmed name. The (kind, tenant, object) unique
    constraint is the backstop for two racing registrations; the lock makes the first check exact.
    """
    session = uow.session
    await lock_authz_writes(session)
    owner = await _owner_for_write(session, actor, owner_folder_id)
    existing = await session.scalar(
        select(EntraRegistration.id).where(
            EntraRegistration.kind == "group",
            EntraRegistration.tenant_id == tenant_id,
            EntraRegistration.object_id == object_id,
        )
    )
    if existing is not None:
        raise _conflict("This Entra group is already registered.")
    row = EntraRegistration(
        kind="group",
        tenant_id=tenant_id,
        object_id=object_id,
        owner_folder_id=owner.id,
        display_name=_display(display_name, object_id),
        directly_registered=True,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise _conflict("This Entra group is already registered.") from exc
    await session.refresh(row)
    uow.audit(
        "entra_registration.created",
        "entra_registration",
        row.id,
        before=None,
        after=UnitOfWork.snapshot(row),
        scope_path=owner.ancestor_ids,
    )
    await uow.commit()
    return row


async def unregister_group(uow: UnitOfWork, actor: Principal, registration_id: uuid.UUID) -> None:
    """Delete a group registration with every grant made to its GUID; one bounded event.

    Admission through the group ends with the row, and the grants go in the same transaction so a
    later re-registration cannot resurrect them (D-57). The ceiling applies as for revoking each
    grant (D-32). The event's ``before`` carries the registration snapshot, ``removed_grants`` and
    at most ``REMOVED_GRANT_IDS_LIMIT`` ``removed_grant_ids`` (events stay small).
    """
    session = uow.session
    await lock_authz_writes(session)
    row = (
        await session.execute(
            select(EntraRegistration)
            .where(EntraRegistration.id == registration_id, EntraRegistration.kind == "group")
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or row.owner_folder_id is None:
        raise _not_found()
    owner = await load_folder_facts(session, row.owner_folder_id)
    if owner is None:  # pragma: no cover - the foreign key guarantees the folder
        raise _not_found()
    await require_permission(session, actor, owner.id, "entra:manage")

    grants = (RoleAssignment.principal_type == "entra_group") & (
        RoleAssignment.principal_id == row.object_id
    )
    granted = (
        await session.execute(
            select(RoleAssignment.id, RoleAssignment.folder_id, RoleAssignment.role)
            .where(grants)
            .order_by(RoleAssignment.id)
        )
    ).all()
    grantees = grantees_for(actor)
    held_at: dict[uuid.UUID, frozenset[str]] = {}
    for _grant_id, folder_id, role in granted:
        if folder_id not in held_at:
            held_at[folder_id] = await effective_roles(session, grantees, folder_id)
        check_ceiling(held_at[folder_id], role)
    before = {
        **UnitOfWork.snapshot(row),
        "removed_grants": len(granted),
        "removed_grant_ids": [str(g[0]) for g in granted[:REMOVED_GRANT_IDS_LIMIT]],
    }
    await session.execute(delete(RoleAssignment).where(grants))
    await session.delete(row)
    uow.audit(
        "entra_registration.deleted",
        "entra_registration",
        registration_id,
        before=before,
        after=None,
        scope_path=owner.ancestor_ids,
    )
    await uow.commit()


async def list_registrations(
    session: AsyncSession,
    actor: Principal,
    kind: str,
    *,
    limit: int,
    after: uuid.UUID | None,
) -> list[EntraRegistration]:
    """Up to ``limit + 1`` registrations of ``kind`` after ``after``, in id (creation) order.

    Trimmed to owners where the caller holds ``entra:read``; a Global admin at ROOT sees all. A
    caller who can read nothing gets an empty list, never an error (D-35). Two rows never compare
    equal: the id is the key.
    """
    stmt = select(EntraRegistration).where(EntraRegistration.kind == kind)
    grantees = grantees_for(actor)
    if "global_admin" not in await effective_roles(
        session, grantees, await root_folder_id(session)
    ):
        visible = visible_folders_subquery(grantees, roles_with("entra:read"))
        stmt = stmt.where(EntraRegistration.owner_folder_id.in_(select(visible.c.id)))
    if after is not None:
        stmt = stmt.where(EntraRegistration.id > after)
    stmt = stmt.order_by(EntraRegistration.id).limit(limit + 1)
    return list((await session.execute(stmt)).scalars())


async def register_service_principal(
    uow: UnitOfWork,
    actor: Principal,
    *,
    tenant_id: str,
    owner_folder_id: uuid.UUID,
    object_id: str,
    display_name: str,
) -> EntraRegistration:
    """Register a service principal owned by ROOT or an Organization (D-59, D-73).

    A principal admitted only through a registered group already has a just-in-time row (Plan 04,
    ``directly_registered`` false). Registering it with the same owner turns that row into a direct
    registration (one ``entra_registration.updated``); another owner, or an already direct row, is
    a 409. Otherwise a new row is created (one ``entra_registration.created``). The advisory lock
    on the principal serialises this with the just-in-time writer.
    """
    session = uow.session
    await lock_authz_writes(session)
    owner = await _owner_for_write(session, actor, owner_folder_id)
    await session.execute(_LOCK, {"key": f"entra-sp:{tenant_id}:{object_id}"})
    row = (
        await session.execute(
            select(EntraRegistration)
            .where(
                EntraRegistration.kind == "service_principal",
                EntraRegistration.tenant_id == tenant_id,
                EntraRegistration.object_id == object_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    scope_path = owner.ancestor_ids
    if row is None:
        row = EntraRegistration(
            kind="service_principal",
            tenant_id=tenant_id,
            object_id=object_id,
            owner_folder_id=owner.id,
            display_name=_display(display_name, object_id),
            directly_registered=True,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise _conflict("This service principal is already registered.") from exc
        await session.refresh(row)
        uow.audit(
            "entra_registration.created",
            "entra_registration",
            row.id,
            before=None,
            after=UnitOfWork.snapshot(row),
            scope_path=scope_path,
        )
    elif row.directly_registered or row.owner_folder_id != owner.id:
        raise _conflict("This service principal is already registered.")
    else:
        before = UnitOfWork.snapshot(row)
        row.directly_registered = True
        await session.flush()
        await session.refresh(row)
        uow.audit(
            "entra_registration.updated",
            "entra_registration",
            row.id,
            before=before,
            after=UnitOfWork.snapshot(row),
            scope_path=scope_path,
        )
    await uow.commit()
    return row


async def register_user(
    uow: UnitOfWork,
    actor: Principal,
    *,
    tenant_id: str,
    object_id: str,
    display_name: str,
) -> EntraUser:
    """Register an individual Entra user (Global admin only, D-58, D-59).

    Creates the user's ``entra_users`` record now, or reuses the one a first sign-in already made,
    so direct grants can be made before the first sign-in. One ``entra_registration.created``
    whose ``after`` carries the registration snapshot plus ``entra_user_id``.
    """
    session = uow.session
    await lock_authz_writes(session)
    root = await root_folder_id(session)
    await require_permission(session, actor, root, "user:create")
    # The same key as ``ensure_entra_user``: a first sign-in racing this registration queues.
    await session.execute(_LOCK, {"key": f"entra-user:{tenant_id}:{object_id}"})
    existing = await session.scalar(
        select(EntraRegistration.id).where(
            EntraRegistration.kind == "user",
            EntraRegistration.tenant_id == tenant_id,
            EntraRegistration.object_id == object_id,
        )
    )
    if existing is not None:
        raise _conflict("This Entra user is already registered.")
    user = (
        await session.execute(
            select(EntraUser)
            .where(EntraUser.tenant_id == tenant_id, EntraUser.object_id == object_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if user is None:
        user = EntraUser(
            tenant_id=tenant_id,
            object_id=object_id,
            display_name=_display(display_name, object_id, _DISPLAY_MAX),
        )
        session.add(user)
    registration = EntraRegistration(
        kind="user",
        tenant_id=tenant_id,
        object_id=object_id,
        owner_folder_id=None,
        display_name=_display(display_name, object_id),
        directly_registered=True,
    )
    session.add(registration)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise _conflict("This Entra user is already registered.") from exc
    await session.refresh(registration)
    await session.refresh(user)
    uow.audit(
        "entra_registration.created",
        "entra_registration",
        registration.id,
        before=None,
        after={**UnitOfWork.snapshot(registration), "entra_user_id": str(user.id)},
        scope_path=[root],
    )
    await uow.commit()
    return user


async def registered_object_ids(
    session: AsyncSession,
    kind: str,
    tenant_id: str,
    object_ids: Sequence[str],
    owner_folder_id: uuid.UUID | None,
) -> frozenset[str]:
    """The object ids for which the matching register call would answer 409 (one SELECT).

    A user or group counts once a registration row of that kind exists. A service principal counts
    when its row is directly registered or belongs to another owner than ``owner_folder_id`` (a
    just-in-time row of the same owner can still be registered, as ``register_service_principal``
    allows). Read only: no lock, no audit.
    """
    if not object_ids:
        return frozenset()
    stmt = select(
        EntraRegistration.object_id,
        EntraRegistration.directly_registered,
        EntraRegistration.owner_folder_id,
    ).where(
        EntraRegistration.kind == kind,
        EntraRegistration.tenant_id == tenant_id,
        EntraRegistration.object_id.in_(list(object_ids)),
    )
    taken: set[str] = set()
    for object_id, direct, owner in (await session.execute(stmt)).all():
        if kind != "service_principal" or direct or owner != owner_folder_id:
            taken.add(object_id)
    return frozenset(taken)


async def known_group_members(
    session: AsyncSession,
    actor: Principal,
    tenant_id: str,
    *,
    user_ids: Sequence[str],
    service_principal_ids: Sequence[str],
) -> dict[tuple[str, str], uuid.UUID]:
    """The Contractum record ids of directory members the caller may open (quick 261119-wp6).

    Keys are ``("user", object_id)`` (the ``entra_users`` id, only for a caller who holds
    ``user:read`` at ROOT) and ``("service_principal", object_id)`` (the registration id, direct or
    just-in-time, only where the caller holds ``entra:read`` at its owner or is a Global admin).
    A member the caller may not open is simply absent, so the answer never reveals a record the
    caller could not already read. Read only: no lock, no audit, no denial row.
    """
    grantees = grantees_for(actor)
    root = await root_folder_id(session)
    root_roles = await effective_roles(session, grantees, root)
    found: dict[tuple[str, str], uuid.UUID] = {}
    if user_ids and (root_roles & roles_with("user:read")):
        rows = await session.execute(
            select(EntraUser.object_id, EntraUser.id).where(
                EntraUser.tenant_id == tenant_id, EntraUser.object_id.in_(list(user_ids))
            )
        )
        for object_id, record_id in rows.all():
            found[("user", object_id)] = record_id
    if service_principal_ids:
        stmt = select(EntraRegistration.object_id, EntraRegistration.id).where(
            EntraRegistration.kind == "service_principal",
            EntraRegistration.tenant_id == tenant_id,
            EntraRegistration.object_id.in_(list(service_principal_ids)),
        )
        if "global_admin" not in root_roles:
            visible = visible_folders_subquery(grantees, roles_with("entra:read"))
            stmt = stmt.where(EntraRegistration.owner_folder_id.in_(select(visible.c.id)))
        for object_id, record_id in (await session.execute(stmt)).all():
            found[("service_principal", object_id)] = record_id
    return found


_USER_REGISTERED_DIRECTLY = (
    exists()
    .where(
        EntraRegistration.kind == "user",
        EntraRegistration.tenant_id == EntraUser.tenant_id,
        EntraRegistration.object_id == EntraUser.object_id,
        EntraRegistration.is_active.is_(True),
    )
    .correlate(EntraUser)
)


async def list_entra_users(
    session: AsyncSession, *, limit: int, after: uuid.UUID | None
) -> list[tuple[EntraUser, bool]]:
    """Up to ``limit + 1`` Entra users after ``after`` in id order, with ``registered_directly``.

    ``registered_directly`` is true while an active ``user`` registration exists for the record.
    """
    stmt = select(EntraUser, _USER_REGISTERED_DIRECTLY)
    if after is not None:
        stmt = stmt.where(EntraUser.id > after)
    stmt = stmt.order_by(EntraUser.id).limit(limit + 1)
    return [(row[0], bool(row[1])) for row in (await session.execute(stmt)).all()]


async def get_entra_user(
    session: AsyncSession, entra_user_id: uuid.UUID
) -> tuple[EntraUser, bool] | None:
    row = (
        await session.execute(
            select(EntraUser, _USER_REGISTERED_DIRECTLY).where(EntraUser.id == entra_user_id)
        )
    ).one_or_none()
    return None if row is None else (row[0], bool(row[1]))
