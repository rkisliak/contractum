"""Idempotent bootstrap of ROOT, the first Global admin and the bootstrap state (D-01, D-02).

Runs in the identity lifespan under a transaction-scoped advisory lock, as the system actor, and
emits exactly one ``system.bootstrapped`` event the first time it creates anything. The secret
file is validated but its value is never logged, audited or returned.

``ensure_notifier`` (D-105) then provisions the contracts delivery worker's identity: a ROOT-owned
service account, one key built from a public id and hash read from a Docker secret file (identity
never sees the plaintext), and a ROOT grant of the ``notifier`` role. One
``system.notifier_provisioned`` event records a change; a restart that changes nothing records
none. The file content, the public id and the hash are never logged or audited.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog
import uuid_utils.compat as uuid_compat
from dc_auth.roles import Role
from dc_core.actor import system_actor
from dc_core.app import LifespanHook
from dc_core.uow import UnitOfWork
from sqlalchemy import Integer, cast, func, select, text

from dc_identity.authz import GLOBAL_ADMIN, LOCAL_PREFIX
from dc_identity.credentials import LIVE, SERVICE_ACCOUNT_KEY_KIND
from dc_identity.models import (
    ApiCredential,
    BootstrapState,
    Folder,
    OutboxEvent,
    RoleAssignment,
    ServiceAccount,
    User,
)
from dc_identity.principal_ids import service_account_principal_id

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dc_identity.settings import IdentitySettings

_log = structlog.get_logger("dc_identity.bootstrap")

MIN_SECRET_LENGTH = 32
SecretProblem = Literal["missing", "too_short", "unreadable"]


def read_bootstrap_secret(path: Path) -> tuple[str | None, SecretProblem | None]:
    """The bootstrap secret, or ``(None, reason)``. The value is never logged here."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeDecodeError):
        return None, "unreadable"
    secret = raw.removesuffix("\n").removesuffix("\r")
    if len(secret) < MIN_SECRET_LENGTH:
        return None, "too_short"
    return secret, None


async def ensure_bootstrap(
    session_factory: async_sessionmaker[AsyncSession], settings: IdentitySettings
) -> None:
    """Create whatever of the bootstrap state is missing; a restart changes nothing."""
    _, problem = read_bootstrap_secret(settings.bootstrap_secret_file)
    if problem is not None:
        # Identity still starts; login answers bootstrap-unavailable (D-01). Reason only.
        _log.warning("bootstrap_secret_unavailable", reason=problem)

    uow = UnitOfWork(
        session_factory,
        actor=system_actor("bootstrap"),
        outbox_model=OutboxEvent,
        service="identity",
    )
    async with uow:
        session = uow.session
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('dc-identity-bootstrap'))")
        )
        username = settings.bootstrap_admin_username.strip().lower()
        created = False

        root = (
            await session.execute(select(Folder).where(Folder.kind == "root"))
        ).scalar_one_or_none()
        if root is None:
            root = Folder(
                id=uuid_compat.uuid7(),
                kind="root",
                slug="root",
                display_name="ROOT",
                path="root",
            )
            session.add(root)
            created = True

        admin = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if admin is None:
            admin = User(
                id=uuid_compat.uuid7(),
                username=username,
                display_name=username,
                password_hash=None,
                must_rotate=True,
                is_active=True,
            )
            session.add(admin)
            created = True

        principal_id = f"{LOCAL_PREFIX}{admin.id}"
        grant = (
            await session.execute(
                select(RoleAssignment).where(
                    RoleAssignment.folder_id == root.id,
                    RoleAssignment.principal_type == "user",
                    RoleAssignment.principal_id == principal_id,
                    RoleAssignment.role == GLOBAL_ADMIN,
                )
            )
        ).scalar_one_or_none()
        if grant is None:
            session.add(
                RoleAssignment(
                    id=uuid_compat.uuid7(),
                    folder_id=root.id,
                    principal_type="user",
                    principal_id=principal_id,
                    role=GLOBAL_ADMIN,
                )
            )
            created = True

        state = (
            await session.execute(select(BootstrapState).where(BootstrapState.id == 1))
        ).scalar_one_or_none()
        if state is None:
            session.add(BootstrapState(id=1))
            created = True

        if not created:
            return

        uow.audit(
            "system.bootstrapped",
            "system",
            root.id,
            before=None,
            after={
                "root_folder_id": str(root.id),
                "admin_user_id": str(admin.id),
                "bootstrap_enabled": problem is None,
            },
            scope_path=[root.id],
        )
        await uow.commit()


NotifierProblem = Literal["missing", "unreadable", "malformed"]
_PUBLIC_ID = re.compile(r"[0-9a-f]{16}")
_SECRET_HASH = re.compile(r"[0-9a-f]{64}")
# The name every key this module inserts carries, so a rotation can find the previous one.
_NOTIFIER_KEY_NAME = "bootstrap"


def read_notifier_credential(path: Path) -> tuple[tuple[str, str] | None, NotifierProblem | None]:
    """The notifier ``(public_id, secret_hash)`` from its file, or ``(None, reason)``.

    One line ``<16 hex public id>:<64 hex SHA-256>``; a single trailing newline is ignored. Only a
    reason is ever returned, never any part of the content.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeDecodeError):
        return None, "unreadable"
    line = raw.removesuffix("\n").removesuffix("\r")
    public_id, separator, secret_hash = line.partition(":")
    if (
        not separator
        or _PUBLIC_ID.fullmatch(public_id) is None
        or _SECRET_HASH.fullmatch(secret_hash) is None
    ):
        return None, "malformed"
    return (public_id, secret_hash), None


async def ensure_notifier(
    session_factory: async_sessionmaker[AsyncSession], settings: IdentitySettings
) -> None:
    """Provision the contracts notifier from its credential file; a restart changes nothing.

    A missing, unreadable or malformed file never stops identity: it logs the reason and returns.
    """
    credential, problem = read_notifier_credential(settings.notifier_credential_file)
    if credential is None:
        _log.warning("notifier_credential_unavailable", reason=problem)
        return
    public_id, secret_hash = credential

    uow = UnitOfWork(
        session_factory,
        actor=system_actor("bootstrap"),
        outbox_model=OutboxEvent,
        service="identity",
    )
    async with uow:
        session = uow.session
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('dc-identity-bootstrap'))")
        )
        root = (
            await session.execute(select(Folder).where(Folder.kind == "root"))
        ).scalar_one_or_none()
        if root is None:
            # ensure_bootstrap runs first and creates ROOT; without it there is nothing to own.
            _log.warning("notifier_credential_unavailable", reason="no_root")
            return

        name = settings.notifier_account_name.strip()
        account = (
            await session.execute(
                select(ServiceAccount).where(
                    ServiceAccount.owner_folder_id == root.id,
                    func.lower(ServiceAccount.name) == name.lower(),
                )
            )
        ).scalar_one_or_none()
        changed = False
        if account is None:
            account = ServiceAccount(
                id=uuid_compat.uuid7(), owner_folder_id=root.id, name=name, is_active=True
            )
            session.add(account)
            changed = True
        elif not account.is_active:
            # Reactivation is an operator decision; bootstrap never reverses a deactivation.
            _log.warning("notifier_account_inactive")
            return

        existing = (
            await session.execute(select(ApiCredential).where(ApiCredential.public_id == public_id))
        ).scalar_one_or_none()
        revoked_ids: list[uuid.UUID] = []
        renewed_id: uuid.UUID | None = None
        renewal_reason: Literal["expired", "expiring"] | None = None
        lifetime = func.make_interval(0, 0, 0, cast(settings.notifier_key_lifetime_days, Integer))
        if existing is not None and existing.service_account_id != account.id:
            # The id belongs to another credential (a PAT or another account's key): never touch it.
            _log.warning("notifier_credential_conflict")
            return
        if existing is None:
            credential_row = ApiCredential(
                id=uuid_compat.uuid7(),
                public_id=public_id,
                secret_hash=secret_hash,
                kind=SERVICE_ACCOUNT_KEY_KIND,
                service_account_id=account.id,
                name=_NOTIFIER_KEY_NAME,
                read_only=False,
                expires_at=func.now() + lifetime,
            )
            # Rotation: the key a previous file provisioned stops working in the same unit.
            previous = (
                (
                    await session.execute(
                        select(ApiCredential).where(
                            ApiCredential.service_account_id == account.id,
                            ApiCredential.name == _NOTIFIER_KEY_NAME,
                            LIVE,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for old in previous:
                old.revoked_at = func.now()
                revoked_ids.append(old.id)
            session.add(credential_row)
            changed = True
        else:
            credential_row = existing
            if existing.revoked_at is not None:
                # An operator revoked it: never revived. Recovery takes a new pair in the file.
                _log.warning("notifier_credential_revoked")
                return
            # Decided on the database clock, so the app host's time does not matter.
            warning = func.make_interval(
                0, 0, 0, cast(settings.credential_expiry_warning_days, Integer)
            )
            expired, expiring = (
                await session.execute(
                    select(
                        ApiCredential.expires_at <= func.now(),
                        ApiCredential.expires_at <= func.now() + warning,
                    ).where(ApiCredential.id == existing.id)
                )
            ).one()
            if expiring:
                # Same row, same public id: the key authenticates again with no new file.
                existing.expires_at = func.now() + lifetime
                renewed_id = existing.id
                renewal_reason = "expired" if expired else "expiring"
                changed = True

        principal_id = service_account_principal_id(account.id)
        grant = (
            await session.execute(
                select(RoleAssignment).where(
                    RoleAssignment.folder_id == root.id,
                    RoleAssignment.principal_type == "service_account",
                    RoleAssignment.principal_id == principal_id,
                    RoleAssignment.role == Role.NOTIFIER.value,
                )
            )
        ).scalar_one_or_none()
        if grant is None:
            session.add(
                RoleAssignment(
                    id=uuid_compat.uuid7(),
                    folder_id=root.id,
                    principal_type="service_account",
                    principal_id=principal_id,
                    role=Role.NOTIFIER.value,
                )
            )
            changed = True

        if not changed:
            return

        uow.audit(
            "system.notifier_provisioned",
            "system",
            root.id,
            before=None,
            after={
                "service_account_id": str(account.id),
                "credential_id": str(credential_row.id),
                "role": Role.NOTIFIER.value,
                "revoked_credential_ids": [str(i) for i in revoked_ids],
                "renewed_credential_id": str(renewed_id) if renewed_id is not None else None,
            },
            scope_path=[root.id],
        )
        await uow.commit()
    if renewal_reason is not None:
        _log.warning("notifier_credential_renewed", reason=renewal_reason)


async def _renew_notifier_periodically(
    session_factory: async_sessionmaker[AsyncSession], settings: IdentitySettings
) -> None:
    """Re-run ``ensure_notifier`` on an interval, for the life of the process (WR-05).

    ``ensure_notifier`` is idempotent and serialised by a transaction-scoped advisory lock, so every
    replica may run it: it renews the key only inside the warning window and otherwise changes
    nothing. A failure is logged by class and retried at the next tick.
    """
    while True:
        await asyncio.sleep(settings.notifier_renewal_interval_s)
        try:
            await ensure_notifier(session_factory, settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("notifier_renewal_failed", error=type(exc).__name__)


def bootstrap_hook(settings: IdentitySettings) -> LifespanHook:
    """Lifespan hook: ``ensure_bootstrap`` and ``ensure_notifier`` before serving, then a task that
    keeps the notifier key renewed while identity runs."""

    @asynccontextmanager
    async def _hook(app: FastAPI) -> AsyncIterator[None]:
        await ensure_bootstrap(app.state.session_factory, settings)
        await ensure_notifier(app.state.session_factory, settings)
        task = asyncio.create_task(
            _renew_notifier_periodically(app.state.session_factory, settings),
            name="notifier-key-renewal",
        )
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return _hook


__all__ = [
    "bootstrap_hook",
    "ensure_bootstrap",
    "ensure_notifier",
    "read_bootstrap_secret",
    "read_notifier_credential",
]
