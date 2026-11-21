"""Service account domain service: owned, audited pipeline principals (AUTH-07, D-69, D-78).

A service account is owned by ROOT or an Organization, exactly like a local group (D-27), and its
principal id is ``sa:<uuid>`` (D-78). Permissions are evaluated at the owner folder by the ordinary
``require()`` gate, so an Organization admin manages only its own Organization's accounts. Each
write takes the authorization-write lock, re-checks its permission under it and records exactly one
audit event. Keys live in :mod:`dc_identity.credentials`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from dc_auth.roles import Role, roles_with
from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from dc_identity.access import effective_roles, grantees_for, visible_folders_subquery
from dc_identity.authz import load_folder_facts
from dc_identity.entra import check_owner_kind
from dc_identity.grants import _not_found
from dc_identity.guards import lock_authz_writes, reject_archived, require_permission
from dc_identity.models import ServiceAccount
from dc_identity.sessions import root_folder_id

if TYPE_CHECKING:
    from dc_auth.principal import Principal
    from sqlalchemy.ext.asyncio import AsyncSession

_DUPLICATE_NAME = "uq_service_accounts_owner_lower_name"


def _conflict(detail: str) -> ProblemError:
    return ProblemError(409, "conflict", "Conflict", detail)


async def service_account_owner(service_account_id: uuid.UUID, session: AsyncSession) -> uuid.UUID:
    """Route target resolver: the owner folder; unknown is the same 404 as unreadable."""
    owner_id = (
        await session.execute(
            select(ServiceAccount.owner_folder_id).where(ServiceAccount.id == service_account_id)
        )
    ).scalar_one_or_none()
    if owner_id is None:
        raise _not_found()
    return owner_id


async def create_service_account(
    uow: UnitOfWork, actor: Principal, *, owner_folder_id: uuid.UUID, name: str
) -> ServiceAccount:
    """Create an account owned by ROOT or an Organization; one ``service_account.created`` event.

    The owner kind is checked first (422 ``owner-not-allowed``), then ``service-account:create`` is
    re-checked at the owner under the authorization-write lock, so a revocation that committed
    after the gate cannot be outrun.
    """
    session = uow.session
    owner = await load_folder_facts(session, owner_folder_id)
    if owner is None:
        raise _not_found()
    check_owner_kind(owner)
    await lock_authz_writes(session)
    await require_permission(session, actor, owner.id, "service-account:create")
    reject_archived(owner)
    account = ServiceAccount(owner_folder_id=owner.id, name=name)
    # No SAVEPOINT: the unique index is the arbiter and the 409 rolls the unit of work back.
    session.add(account)
    try:
        await session.flush()
    except IntegrityError as exc:
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == _DUPLICATE_NAME:
            raise _conflict("A service account with this name already exists here.") from exc
        raise
    await session.refresh(account)
    uow.audit(
        "service_account.created",
        "service_account",
        account.id,
        before=None,
        after=UnitOfWork.snapshot(account),
        scope_path=owner.ancestor_ids,
    )
    await uow.commit()
    return account


async def rename_service_account(
    uow: UnitOfWork, actor: Principal, service_account_id: uuid.UUID, *, name: str
) -> ServiceAccount:
    """Rename an account; no event when unchanged, else one ``service_account.renamed``."""
    session = uow.session
    account = (
        await session.execute(
            select(ServiceAccount).where(ServiceAccount.id == service_account_id).with_for_update()
        )
    ).scalar_one_or_none()
    if account is None:
        raise _not_found()
    await lock_authz_writes(session)
    await require_permission(session, actor, account.owner_folder_id, "service-account:manage")
    facts = await load_folder_facts(session, account.owner_folder_id)
    if facts is None:  # pragma: no cover - the foreign key guarantees the folder
        raise _not_found()
    reject_archived(facts)
    if account.name == name:
        return account
    before = UnitOfWork.snapshot(account)
    account.name = name
    try:
        await session.flush()
    except IntegrityError as exc:
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == _DUPLICATE_NAME:
            raise _conflict("A service account with this name already exists here.") from exc
        raise
    await session.refresh(account)
    uow.audit(
        "service_account.renamed",
        "service_account",
        account.id,
        before=before,
        after=UnitOfWork.snapshot(account),
        scope_path=facts.ancestor_ids,
    )
    await uow.commit()
    return account


async def list_service_accounts(
    session: AsyncSession,
    actor: Principal,
    *,
    owner_folder_id: uuid.UUID | None,
    limit: int,
    after: uuid.UUID | None,
) -> list[ServiceAccount]:
    """Up to ``limit + 1`` accounts after ``after`` in id order, trimmed to readable owners.

    A Global admin at ROOT sees all; everyone else sees the accounts whose owner folder holds
    ``service-account:read`` for them. A caller who can read nothing gets an empty list (D-35).
    """
    stmt = select(ServiceAccount)
    grantees = grantees_for(actor)
    if Role.GLOBAL_ADMIN.value not in await effective_roles(
        session, grantees, await root_folder_id(session)
    ):
        visible = visible_folders_subquery(grantees, roles_with("service-account:read"))
        stmt = stmt.where(ServiceAccount.owner_folder_id.in_(select(visible.c.id)))
    if owner_folder_id is not None:
        stmt = stmt.where(ServiceAccount.owner_folder_id == owner_folder_id)
    if after is not None:
        stmt = stmt.where(ServiceAccount.id > after)
    stmt = stmt.order_by(ServiceAccount.id).limit(limit + 1)
    return list((await session.execute(stmt)).scalars())


__all__ = [
    "create_service_account",
    "list_service_accounts",
    "rename_service_account",
    "service_account_owner",
]
