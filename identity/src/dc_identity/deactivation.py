"""Deactivating and reactivating local users (AUTH-08, D-75, D-76, D-34).

Deactivation is one audited cascade in one transaction: the account turns inactive, every refresh
token and every PAT it owns is revoked, and the single ``user.deactivated`` event lists everything
that was revoked. The ``revoked`` object is ``{refresh_tokens, refresh_token_ids, credentials,
credential_ids}`` with complete, uncapped id lists (row ids only, never a public id, secret or
hash). The same shape is used for every principal kind that gains a deactivation later.

The gate refuses an inactive account on every request, so the account's outstanding access JWTs
stop on the next request. Reactivation restores the account and its grants but never a secret: it
flips ``is_active`` and sets the ``sessions_valid_after`` cutoff, and ``revoked_at`` is never
cleared (D-75).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from dc_auth.roles import Role
from dc_core.problems import ProblemError
from sqlalchemy import Text, cast, func, select

from dc_identity.authz import load_folder_facts
from dc_identity.guards import lock_authz_writes, require_permission
from dc_identity.models import (
    ApiCredential,
    EntraRegistration,
    EntraUser,
    Folder,
    RefreshToken,
    RoleAssignment,
    ServiceAccount,
    User,
)
from dc_identity.sessions import revoke_all_for_user, root_folder_id

if TYPE_CHECKING:
    import uuid

    from dc_auth.principal import Principal
    from dc_core.uow import UnitOfWork
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import InstrumentedAttribute


def _not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not Found", "The resource was not found.")


async def ensure_not_last_active_global_admin(session: AsyncSession, user_id: uuid.UUID) -> None:
    """D-34, D-76: refuse to deactivate the last active local user holding Global admin directly.

    Applies only when ``user_id`` itself holds a direct ROOT ``global_admin`` grant; counts the
    other active local users holding one. Call under :func:`lock_authz_writes`.
    """
    holder = f"local:{user_id}"
    direct = (
        select(RoleAssignment.id)
        .join(Folder, Folder.id == RoleAssignment.folder_id)
        .where(
            Folder.kind == "root",
            RoleAssignment.principal_type == "user",
            RoleAssignment.principal_id == holder,
            RoleAssignment.role == Role.GLOBAL_ADMIN.value,
        )
    )
    if (await session.execute(direct.limit(1))).first() is None:
        return
    others = (
        await session.execute(
            select(func.count())
            .select_from(RoleAssignment)
            .join(Folder, Folder.id == RoleAssignment.folder_id)
            .join(User, RoleAssignment.principal_id == func.concat("local:", cast(User.id, Text)))
            .where(
                Folder.kind == "root",
                RoleAssignment.principal_type == "user",
                RoleAssignment.role == Role.GLOBAL_ADMIN.value,
                User.id != user_id,
                User.is_active.is_(True),
            )
        )
    ).scalar_one()
    if others == 0:
        raise ProblemError(
            409,
            "last-global-admin",
            "Conflict",
            "The last Global admin cannot be deactivated.",
        )


async def _revoke_credentials(
    session: AsyncSession, *, column: InstrumentedAttribute[uuid.UUID | None], owner_id: uuid.UUID
) -> list[str]:
    """Revoke every unrevoked credential of one owner; returns every id it revoked, in id order.

    ``column`` is the owner column (``ApiCredential.local_user_id``, ``entra_user_id`` or
    ``service_account_id``). The list is complete and uncapped: the deactivation event lists
    everything it revoked (D-75).
    """
    rows = list(
        (
            await session.execute(
                select(ApiCredential)
                .where(column == owner_id, ApiCredential.revoked_at.is_(None))
                .order_by(ApiCredential.id)
                .with_for_update()
            )
        ).scalars()
    )
    for row in rows:
        row.revoked_at = func.now()
    await session.flush()
    return [str(row.id) for row in rows]


async def _revoked_refresh_ids(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    """Ids of every refresh-token row of the user that this transaction revoked, in id order.

    ``revoke_all_for_user`` writes ``revoked_at = now()`` on every unrevoked row, whether live,
    already rotated or already expired, and PostgreSQL's ``now()`` is the transaction timestamp, so
    ``revoked_at = now()`` selects exactly those rows with no liveness filter (D-75).
    """
    ids = (
        await session.execute(
            select(RefreshToken.id)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at == func.now())
            .order_by(RefreshToken.id)
        )
    ).scalars()
    return [str(i) for i in ids]


async def _locked_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = (
        await session.execute(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None:
        raise _not_found()
    return user


async def deactivate_local_user(uow: UnitOfWork, actor: Principal, user_id: uuid.UUID) -> User:
    """Deactivate a local user and revoke all its refresh tokens and PATs; one ``user.deactivated``.

    Already inactive: returned unchanged with no write. The last active direct Global admin is a
    409 ``last-global-admin``.
    """
    session = uow.session
    await lock_authz_writes(session)
    root = await root_folder_id(session)
    await require_permission(session, actor, root, "user:deactivate")
    user = await _locked_user(session, user_id)
    if not user.is_active:
        return user
    await ensure_not_last_active_global_admin(session, user.id)
    before = uow.snapshot(user)
    user.is_active = False
    await revoke_all_for_user(session, user.id)  # its return value counts only the live rows
    refresh_ids = await _revoked_refresh_ids(session, user.id)
    credential_ids = await _revoke_credentials(
        session, column=ApiCredential.local_user_id, owner_id=user.id
    )
    await session.flush()
    await session.refresh(user)
    uow.audit(
        "user.deactivated",
        "user",
        user.id,
        before=before,
        after={
            **uow.snapshot(user),
            "revoked": {
                "refresh_tokens": len(refresh_ids),
                "refresh_token_ids": refresh_ids,
                "credentials": len(credential_ids),
                "credential_ids": credential_ids,
            },
        },
        scope_path=[root],
    )
    await uow.commit()
    return user


async def reactivate_local_user(uow: UnitOfWork, actor: Principal, user_id: uuid.UUID) -> User:
    """Reactivate a local user; one ``user.reactivated``. Never restores a revoked secret (D-75).

    Already active: returned unchanged with no write.
    """
    session = uow.session
    await lock_authz_writes(session)
    root = await root_folder_id(session)
    await require_permission(session, actor, root, "user:deactivate")
    user = await _locked_user(session, user_id)
    if user.is_active:
        return user
    before = uow.snapshot(user)
    user.is_active = True
    # Session cutoff (D-75): nothing issued at or before now works again. Read after the row lock
    # is held, from the identity process clock that also sets a local JWT's iat. No credential can
    # be minted for an inactive account, so a cutoff here covers a login that raced the
    # deactivation commit; while the account is inactive is_active already blocks everything.
    user.sessions_valid_after = datetime.now(UTC)
    await session.flush()
    await session.refresh(user)
    uow.audit(
        "user.reactivated",
        "user",
        user.id,
        before=before,
        after=uow.snapshot(user),
        scope_path=[root],
    )
    await uow.commit()
    return user


def _revoked(credential_ids: list[str]) -> dict[str, object]:
    """The ``revoked`` object for a kind that owns no refresh tokens (same shape as a user's)."""
    return {
        "refresh_tokens": 0,
        "refresh_token_ids": [],
        "credentials": len(credential_ids),
        "credential_ids": credential_ids,
    }


# --- Entra users (D-75, D-76): Global admin only, like local users ------------------------------


async def _locked_entra_user(session: AsyncSession, entra_user_id: uuid.UUID) -> EntraUser:
    row = (
        await session.execute(
            select(EntraUser)
            .where(EntraUser.id == entra_user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found()
    return row


async def deactivate_entra_user(
    uow: UnitOfWork, actor: Principal, entra_user_id: uuid.UUID
) -> EntraUser:
    """Deactivate an Entra user and revoke its PATs; one ``entra_user.deactivated``.

    The gate reads ``entra_users.is_active`` on every request, so the user's tokens stop on the
    next request without waiting for the directory. Already inactive: unchanged, no write. No
    last-Global-admin guard applies (D-34 protects only local direct Global admins).
    """
    session = uow.session
    await lock_authz_writes(session)
    root = await root_folder_id(session)
    await require_permission(session, actor, root, "user:deactivate")
    row = await _locked_entra_user(session, entra_user_id)
    if not row.is_active:
        return row
    before = uow.snapshot(row)
    row.is_active = False
    credential_ids = await _revoke_credentials(
        session, column=ApiCredential.entra_user_id, owner_id=row.id
    )
    await session.flush()
    await session.refresh(row)
    uow.audit(
        "entra_user.deactivated",
        "entra_user",
        row.id,
        before=before,
        after={**uow.snapshot(row), "revoked": _revoked(credential_ids)},
        scope_path=[root],
    )
    await uow.commit()
    return row


async def reactivate_entra_user(
    uow: UnitOfWork, actor: Principal, entra_user_id: uuid.UUID
) -> EntraUser:
    """Reactivate an Entra user; one ``entra_user.reactivated``. Never restores a PAT (D-75).

    Writes the session cutoff: a token issued at or before now stays refused. Already active:
    unchanged, no write.
    """
    session = uow.session
    await lock_authz_writes(session)
    root = await root_folder_id(session)
    await require_permission(session, actor, root, "user:deactivate")
    row = await _locked_entra_user(session, entra_user_id)
    if row.is_active:
        return row
    before = uow.snapshot(row)
    row.is_active = True
    # Read after the row lock is held; the gate refuses a verified iat at or before it.
    row.sessions_valid_after = datetime.now(UTC)
    await session.flush()
    await session.refresh(row)
    uow.audit(
        "entra_user.reactivated",
        "entra_user",
        row.id,
        before=before,
        after=uow.snapshot(row),
        scope_path=[root],
    )
    await uow.commit()
    return row


# --- Service principals (direct or just-in-time rows), owner-scoped -----------------------------


async def _locked_service_principal(
    session: AsyncSession, registration_id: uuid.UUID
) -> EntraRegistration:
    row = (
        await session.execute(
            select(EntraRegistration)
            .where(
                EntraRegistration.id == registration_id,
                EntraRegistration.kind == "service_principal",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None or row.owner_folder_id is None:
        raise _not_found()
    return row


async def _service_principal_owner(
    session: AsyncSession, actor: Principal, row: EntraRegistration
) -> tuple[uuid.UUID, ...]:
    """Re-check ``entra:manage`` at the row's owner under the lock; returns the scope path."""
    assert row.owner_folder_id is not None  # noqa: S101 - a service principal always has an owner
    facts = await load_folder_facts(session, row.owner_folder_id)
    if facts is None:  # pragma: no cover - the foreign key guarantees the folder
        raise _not_found()
    await require_permission(session, actor, facts.id, "entra:manage")
    return tuple(facts.ancestor_ids)


async def deactivate_service_principal(
    uow: UnitOfWork, actor: Principal, registration_id: uuid.UUID
) -> EntraRegistration:
    """Deactivate a service principal's row (direct or just-in-time); one event.

    The gate refuses an inactive row even when a registered group still admits the principal. A
    service principal owns no credentials, so the ``revoked`` lists are empty. Already inactive:
    unchanged, no write.
    """
    session = uow.session
    await lock_authz_writes(session)
    row = await _locked_service_principal(session, registration_id)
    scope = await _service_principal_owner(session, actor, row)
    if not row.is_active:
        return row
    before = uow.snapshot(row)
    row.is_active = False
    await session.flush()
    await session.refresh(row)
    uow.audit(
        "entra_registration.deactivated",
        "entra_registration",
        row.id,
        before=before,
        after={**uow.snapshot(row), "revoked": _revoked([])},
        scope_path=list(scope),
    )
    await uow.commit()
    return row


async def reactivate_service_principal(
    uow: UnitOfWork, actor: Principal, registration_id: uuid.UUID
) -> EntraRegistration:
    """Reactivate a service principal's row; one ``entra_registration.reactivated``.

    Writes the session cutoff: an app token issued at or before now stays refused (D-75).
    Already active: unchanged, no write.
    """
    session = uow.session
    await lock_authz_writes(session)
    row = await _locked_service_principal(session, registration_id)
    scope = await _service_principal_owner(session, actor, row)
    if row.is_active:
        return row
    before = uow.snapshot(row)
    row.is_active = True
    row.sessions_valid_after = datetime.now(UTC)
    await session.flush()
    await session.refresh(row)
    uow.audit(
        "entra_registration.reactivated",
        "entra_registration",
        row.id,
        before=before,
        after=uow.snapshot(row),
        scope_path=list(scope),
    )
    await uow.commit()
    return row


# --- Service accounts: owner-scoped; keys are opaque rows, so no session cutoff -----------------


async def _locked_service_account(
    session: AsyncSession, service_account_id: uuid.UUID
) -> ServiceAccount:
    row = (
        await session.execute(
            select(ServiceAccount)
            .where(ServiceAccount.id == service_account_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found()
    return row


async def _service_account_scope(
    session: AsyncSession, actor: Principal, account: ServiceAccount
) -> tuple[uuid.UUID, ...]:
    """Re-check ``service-account:deactivate`` at the owner under the lock; the scope path."""
    facts = await load_folder_facts(session, account.owner_folder_id)
    if facts is None:  # pragma: no cover - the foreign key guarantees the folder
        raise _not_found()
    await require_permission(session, actor, facts.id, "service-account:deactivate")
    return tuple(facts.ancestor_ids)


async def deactivate_service_account(
    uow: UnitOfWork, actor: Principal, service_account_id: uuid.UUID
) -> ServiceAccount:
    """Deactivate a service account and revoke every key; one ``service_account.deactivated``.

    Reactivation never revives a key: new keys are created after it (D-75). Already inactive:
    unchanged, no write.
    """
    session = uow.session
    await lock_authz_writes(session)
    account = await _locked_service_account(session, service_account_id)
    scope = await _service_account_scope(session, actor, account)
    if not account.is_active:
        return account
    before = uow.snapshot(account)
    account.is_active = False
    credential_ids = await _revoke_credentials(
        session, column=ApiCredential.service_account_id, owner_id=account.id
    )
    await session.flush()
    await session.refresh(account)
    uow.audit(
        "service_account.deactivated",
        "service_account",
        account.id,
        before=before,
        after={**uow.snapshot(account), "revoked": _revoked(credential_ids)},
        scope_path=list(scope),
    )
    await uow.commit()
    return account


async def reactivate_service_account(
    uow: UnitOfWork, actor: Principal, service_account_id: uuid.UUID
) -> ServiceAccount:
    """Reactivate a service account; one ``service_account.reactivated``. Revoked keys stay so.

    Already active: unchanged, no write.
    """
    session = uow.session
    await lock_authz_writes(session)
    account = await _locked_service_account(session, service_account_id)
    scope = await _service_account_scope(session, actor, account)
    if account.is_active:
        return account
    before = uow.snapshot(account)
    account.is_active = True
    await session.flush()
    await session.refresh(account)
    uow.audit(
        "service_account.reactivated",
        "service_account",
        account.id,
        before=before,
        after=uow.snapshot(account),
        scope_path=list(scope),
    )
    await uow.commit()
    return account


__all__ = [
    "deactivate_entra_user",
    "deactivate_local_user",
    "deactivate_service_account",
    "deactivate_service_principal",
    "ensure_not_last_active_global_admin",
    "reactivate_entra_user",
    "reactivate_local_user",
    "reactivate_service_account",
    "reactivate_service_principal",
]
