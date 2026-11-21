"""Opaque ``dcp_`` credentials on identity: personal access tokens (AUTH-06, D-66 to D-72).

A PAT is ``dcp_<public_id>_<secret>``. The 201 response of ``create_pat`` is the only place the
whole token ever exists: only the SHA-256 of the secret is stored (256-bit secrets need no slow
hash) and no audit event carries the token, the secret, the hash or the public id.

A PAT is never an independent identity. ``LocalOpaqueResolver`` maps a presented token to its
owner's principal id (``local:<uuid>`` or ``entra:<tid>:<oid>``); ``PrincipalGate`` then loads the
credential again and re-proves the owner's liveness and admission on every request, so a PAT can
neither hold a role its owner lacks nor outlive the owner's access. Nothing here is cached (D-72).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import secrets
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from dc_auth.opaque import OPAQUE_ISSUER, parse_dcp
from dc_auth.principal import Principal
from dc_auth.verify import unauthenticated
from dc_core.app import LifespanHook
from dc_core.problems import ProblemError
from sqlalchemy import ARRAY, ColumnElement, DateTime, and_, bindparam, case, func, select, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from dc_identity.authz import load_folder_facts
from dc_identity.grants import _not_found
from dc_identity.guards import require_permission
from dc_identity.models import ApiCredential, EntraUser, ServiceAccount, User
from dc_identity.principal_ids import (
    entra_principal_id,
    local_principal_id,
    parse_principal_id,
    service_account_principal_id,
)
from dc_identity.sessions import root_folder_id

if TYPE_CHECKING:
    from dc_core.uow import UnitOfWork
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dc_identity.settings import IdentitySettings

_log = structlog.get_logger("dc_identity.credentials")

PAT_KIND = "pat"
SERVICE_ACCOUNT_KEY_KIND = "service_account_key"
# D-70: a service account holds at most this many active keys, so a pipeline can rotate.
MAX_ACTIVE_KEYS = 2

# Compared against when the public id is unknown, so a miss costs the same as a wrong secret.
_DUMMY_HASH = hashlib.sha256(b"dc-identity-no-such-credential").hexdigest()


def token_limit_reached(limit: int, active_count: int) -> ProblemError:
    """409 ``token-limit-reached``: the owner holds the effective maximum of active tokens."""
    return ProblemError(
        409,
        "token-limit-reached",
        "Conflict",
        "You already have the maximum number of active access tokens. "
        "Revoke one or let one expire before creating another.",
        limit=limit,
        active_count=active_count,
    )


# A credential is live while it is not revoked and its expiry has not been reached (D-67).
LIVE = and_(ApiCredential.revoked_at.is_(None), ApiCredential.expires_at > func.now())


@dataclass(frozen=True)
class NewCredential:
    """A freshly minted credential: the public id, the one-time token and the stored hash."""

    public_id: str
    token: str
    secret_hash: str


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def new_credential() -> NewCredential:
    public_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    return NewCredential(
        public_id=public_id,
        token=f"dcp_{public_id}_{secret}",
        secret_hash=hash_secret(secret),
    )


def _out_of_range() -> ProblemError:
    return ProblemError(
        422,
        "expiry-out-of-range",
        "Unprocessable Content",
        "The expiry must be in the future and within the allowed maximum.",
    )


def check_expiry(
    expires_at: datetime, settings: IdentitySettings, max_days: int | None = None
) -> None:
    """Refuse an expiry that is naive, not in the future, or beyond the ceiling.

    ``max_days`` is the effective ceiling (the stored ``credential_max_ttl_days`` setting, quick
    261120-0ye, D-38); without it the env value applies.
    """
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise _out_of_range()
    now = datetime.now(UTC)
    ceiling = settings.credential_max_ttl_days if max_days is None else max_days
    if expires_at <= now or expires_at > now + timedelta(days=ceiling):
        raise _out_of_range()


async def create_pat(
    uow: UnitOfWork,
    actor: Principal,
    *,
    name: str,
    expires_at: datetime,
    settings: IdentitySettings,
    read_only: bool = False,
    max_days: int | None = None,
    max_active: int | None = None,
) -> tuple[ApiCredential, str]:
    """Create a PAT owned by the acting human user; returns the row and the one-time token.

    The owner row is locked ``FOR UPDATE`` and ``is_active`` is re-checked under that lock, so a
    creation racing the owner's deactivation (which takes ``FOR UPDATE`` on the same row and then
    revokes) either commits first or sees the inactive owner (D-75). The same lock serialises
    concurrent creations of one owner, so the count of the owner's live PATs against ``max_active``
    (the effective ``max_active_tokens``, quick 261120-lbz, D-03) is exact: at or above it the
    answer is 409 ``token-limit-reached`` before anything is minted. Existing tokens above a
    lowered limit are untouched. ``read_only`` narrows the PAT to the read permissions of its
    owner (D-66).
    """
    check_expiry(expires_at, settings, max_days)
    if actor.type != "user":
        raise ProblemError(
            403, "forbidden", "Forbidden", "You are not permitted to perform this action."
        )
    ref = parse_principal_id(actor.id)
    session = uow.session
    local_user_id: uuid.UUID | None = None
    entra_user_id: uuid.UUID | None = None
    if ref is not None and ref.kind == "local_user":
        local_user = (
            await session.execute(select(User).where(User.id == ref.local_id).with_for_update())
        ).scalar_one_or_none()
        if local_user is None or not local_user.is_active:
            raise unauthenticated()
        local_user_id = local_user.id
    elif ref is not None and ref.kind == "entra":
        entra_user = (
            await session.execute(
                select(EntraUser)
                .where(EntraUser.tenant_id == ref.tenant_id, EntraUser.object_id == ref.object_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if entra_user is None or not entra_user.is_active:
            raise unauthenticated()
        entra_user_id = entra_user.id
    else:
        raise ProblemError(
            403, "forbidden", "Forbidden", "You are not permitted to perform this action."
        )

    if max_active is not None:
        active = await count_live_pats(session, actor)
        if active >= max_active:
            raise token_limit_reached(max_active, active)

    minted = new_credential()
    row = ApiCredential(
        public_id=minted.public_id,
        secret_hash=minted.secret_hash,
        kind=PAT_KIND,
        local_user_id=local_user_id,
        entra_user_id=entra_user_id,
        name=name,
        read_only=read_only,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    uow.audit(
        "credential.created",
        "api_credential",
        row.id,
        before=None,
        after=uow.snapshot(row),
        scope_path=[await root_folder_id(session)],
    )
    await uow.commit()
    return row, minted.token


class LocalOpaqueResolver:
    """Resolves a ``dcp_`` token from identity's own database, with no cache (D-71, D-72).

    The returned principal carries the owner's principal id (a PAT) or the service account's
    ``sa:<uuid>`` (a service-account key); whether the owner or account is still live and admitted
    is decided afterwards by ``PrincipalGate`` on every request. A malformed token, an
    unknown public id, a wrong secret, a revoked or an expired credential are all ``ValueError``
    (the generic 401), and the secret is compared in constant time even when the id is unknown.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve(self, token: str) -> Principal:
        parsed = parse_dcp(token)
        if parsed is None:
            raise ValueError("malformed opaque token")
        public_id, secret = parsed
        async with self._session_factory() as session:
            found = (
                await session.execute(
                    select(ApiCredential, User, EntraUser, ServiceAccount, LIVE.label("live"))
                    .select_from(ApiCredential)
                    .outerjoin(User, User.id == ApiCredential.local_user_id)
                    .outerjoin(EntraUser, EntraUser.id == ApiCredential.entra_user_id)
                    .outerjoin(
                        ServiceAccount, ServiceAccount.id == ApiCredential.service_account_id
                    )
                    .where(ApiCredential.public_id == public_id)
                )
            ).one_or_none()
        stored = found[0].secret_hash if found is not None else _DUMMY_HASH
        matches = hmac.compare_digest(hash_secret(secret), stored)
        if found is None or not matches:
            raise ValueError("opaque token rejected")
        credential, local_user, entra_user, account, live = found
        if not live:
            raise ValueError("opaque token rejected")
        if credential.kind == SERVICE_ACCOUNT_KEY_KIND:
            # Whether the account is still active is the gate's per-request check (T-04-30).
            if account is None:
                raise ValueError("opaque token rejected")
            return Principal(
                id=service_account_principal_id(account.id),
                type="service_account",
                display=account.name,
                issuer=OPAQUE_ISSUER,
                subject=public_id,
                via="api_key",
                scopes=frozenset({"api"}),
                credential_id=public_id,
            )
        if credential.kind != PAT_KIND:
            raise ValueError("opaque token rejected")
        if local_user is not None:
            owner_id = local_principal_id(local_user.id)
            display = local_user.display_name
        elif entra_user is not None:
            owner_id = entra_principal_id(entra_user.tenant_id, entra_user.object_id)
            display = entra_user.display_name
        else:
            raise ValueError("opaque token rejected")
        return Principal(
            id=owner_id,
            type="pat",
            display=display,
            issuer=OPAQUE_ISSUER,
            subject=public_id,
            via="pat",
            scopes=frozenset({"api"}),
            credential_id=public_id,
        )


def _owned_by(actor: Principal) -> ColumnElement[bool] | None:
    """SQL clause: the credential belongs to the human the actor is (or a PAT of theirs acts as).

    ``None`` when the actor owns no PATs (a service principal, a generic-OIDC id): callers then
    answer an empty page or 404, never an error that tells them anything.
    """
    if actor.type not in ("user", "pat"):
        return None
    ref = parse_principal_id(actor.id)
    if ref is None:
        return None
    if ref.kind == "local_user":
        return ApiCredential.local_user_id == ref.local_id
    if ref.kind == "entra":
        return ApiCredential.entra_user_id.in_(
            select(EntraUser.id).where(
                EntraUser.tenant_id == ref.tenant_id, EntraUser.object_id == ref.object_id
            )
        )
    return None


_STATUS = case(
    (ApiCredential.revoked_at.is_not(None), "revoked"),
    (ApiCredential.expires_at <= func.now(), "expired"),
    else_="active",
)


async def count_live_pats(session: AsyncSession, actor: Principal) -> int:
    """How many unrevoked, unexpired PATs the actor owns (0 for a principal that owns none)."""
    owned = _owned_by(actor)
    if owned is None:
        return 0
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(ApiCredential)
                .where(owned, ApiCredential.kind == PAT_KIND, LIVE)
            )
        ).scalar_one()
    )


async def list_own_pats(
    session: AsyncSession,
    actor: Principal,
    *,
    cursor: uuid.UUID | None,
    limit: int,
) -> tuple[list[tuple[ApiCredential, str]], uuid.UUID | None]:
    """The actor's PATs (with their derived status) by id, one keyset page, and the next cursor."""
    owned = _owned_by(actor)
    if owned is None:
        return [], None
    stmt = select(ApiCredential, _STATUS).where(owned, ApiCredential.kind == PAT_KIND)
    if cursor is not None:
        stmt = stmt.where(ApiCredential.id > cursor)
    rows = (await session.execute(stmt.order_by(ApiCredential.id).limit(limit + 1))).all()
    page = [(row[0], str(row[1])) for row in rows[:limit]]
    next_cursor = page[-1][0].id if len(rows) > limit else None
    return page, next_cursor


async def _revoke_locked_pat(uow: UnitOfWork, row: ApiCredential | None) -> None:
    """Revoke a PAT row already read ``FOR UPDATE``: the shared tail of both revoke paths.

    A missing row is the one 404. An already revoked PAT is left alone and writes no second event
    (revoked secrets never return, D-75); otherwise ``revoked_at = now()``, one
    ``credential.revoked`` event and the commit.
    """
    if row is None:
        raise _not_found()
    if row.revoked_at is not None:
        return
    session = uow.session
    before = uow.snapshot(row)
    row.revoked_at = func.now()
    await session.flush()
    await session.refresh(row)
    uow.audit(
        "credential.revoked",
        "api_credential",
        row.id,
        before=before,
        after=uow.snapshot(row),
        scope_path=[await root_folder_id(session)],
    )
    await uow.commit()


async def revoke_own_pat(uow: UnitOfWork, actor: Principal, credential_id: uuid.UUID) -> None:
    """Revoke one of the actor's PATs: ``revoked_at = now()``, one ``credential.revoked`` event.

    Another owner's or an unknown id is 404. A PAT that is already revoked is left alone and
    writes no second event (revoked secrets never return, D-75).
    """
    owned = _owned_by(actor)
    if owned is None:
        raise _not_found()
    session = uow.session
    row = (
        await session.execute(
            select(ApiCredential)
            .where(ApiCredential.id == credential_id, ApiCredential.kind == PAT_KIND, owned)
            .with_for_update()
        )
    ).scalar_one_or_none()
    await _revoke_locked_pat(uow, row)


async def list_all_pats(
    session: AsyncSession,
    *,
    owner: str | None = None,
    status: str | None = None,
    cursor: uuid.UUID | None,
    limit: int,
) -> tuple[list[tuple[ApiCredential, str, str]], uuid.UUID | None]:
    """Every user's PATs for the Global admin (D-68): (row, owner principal id, status) by id.

    ``owner`` filters on a principal id (``local:`` or ``entra:``); an id that names no PAT owner
    answers an empty page. The caller exposes metadata only: never the public id or the hash.
    """
    stmt = (
        select(ApiCredential, User, EntraUser, _STATUS)
        .select_from(ApiCredential)
        .outerjoin(User, User.id == ApiCredential.local_user_id)
        .outerjoin(EntraUser, EntraUser.id == ApiCredential.entra_user_id)
        .where(ApiCredential.kind == PAT_KIND)
    )
    if owner is not None:
        ref = parse_principal_id(owner)
        if ref is not None and ref.kind == "local_user":
            stmt = stmt.where(ApiCredential.local_user_id == ref.local_id)
        elif ref is not None and ref.kind == "entra":
            stmt = stmt.where(
                EntraUser.tenant_id == ref.tenant_id, EntraUser.object_id == ref.object_id
            )
        else:
            return [], None
    if status is not None:
        stmt = stmt.where(_STATUS == status)
    if cursor is not None:
        stmt = stmt.where(ApiCredential.id > cursor)
    rows = (await session.execute(stmt.order_by(ApiCredential.id).limit(limit + 1))).all()
    page: list[tuple[ApiCredential, str, str]] = []
    for credential, local_user, entra_user, state in rows[:limit]:
        if local_user is not None:
            owner_id = local_principal_id(local_user.id)
        elif entra_user is not None:
            owner_id = entra_principal_id(entra_user.tenant_id, entra_user.object_id)
        else:  # unreachable: a PAT has exactly one owner (check constraint)
            continue
        page.append((credential, owner_id, str(state)))
    next_cursor = rows[limit - 1][0].id if len(rows) > limit else None
    return page, next_cursor


async def revoke_any_pat(uow: UnitOfWork, actor: Principal, credential_id: uuid.UUID) -> None:
    """Revoke any user's PAT as a Global admin: one ``credential.revoked`` event (D-68, D-79).

    Authorization (``pat:revoke-any``, interactive sign-in only) is the route's job. An unknown id
    is 404; an already revoked PAT is left alone and writes no second event.
    """
    session = uow.session
    row = (
        await session.execute(
            select(ApiCredential)
            .where(ApiCredential.id == credential_id, ApiCredential.kind == PAT_KIND)
            .with_for_update()
        )
    ).scalar_one_or_none()
    await _revoke_locked_pat(uow, row)


async def create_service_account_key(
    uow: UnitOfWork,
    actor: Principal,
    service_account_id: uuid.UUID,
    *,
    name: str,
    expires_at: datetime,
    settings: IdentitySettings,
    max_days: int | None = None,
    max_active: int | None = None,
) -> tuple[ApiCredential, str]:
    """Create a key for a service account; returns the row and the one-time token (D-70).

    The account row is locked ``FOR UPDATE`` before its live keys are counted, so concurrent
    creations queue behind each other and each sees every earlier commit: the account never holds
    more than ``MAX_ACTIVE_KEYS`` active (unrevoked, unexpired) keys, however many requests race
    (T-04-28). An inactive account takes no new key. Under the same lock the account's live keys
    are also counted against ``max_active`` (the effective ``max_active_tokens``, quick
    261120-lbz, D-03): at that limit, when it is below ``MAX_ACTIVE_KEYS``, the answer is 409
    ``token-limit-reached``; at ``MAX_ACTIVE_KEYS`` it stays ``credential-limit-reached``.
    """
    check_expiry(expires_at, settings, max_days)
    session = uow.session
    account = (
        await session.execute(
            select(ServiceAccount).where(ServiceAccount.id == service_account_id).with_for_update()
        )
    ).scalar_one_or_none()
    if account is None:
        raise _not_found()
    # The gate ran in its own session before this handler; re-check under the row lock so a
    # revocation that committed in between is not outrun.
    await require_permission(session, actor, account.owner_folder_id, "service-account:manage-keys")
    if not account.is_active:
        raise ProblemError(409, "conflict", "Conflict", "The service account is deactivated.")
    live_keys = (
        await session.execute(
            select(func.count())
            .select_from(ApiCredential)
            .where(
                ApiCredential.service_account_id == account.id,
                ApiCredential.kind == SERVICE_ACCOUNT_KEY_KIND,
                LIVE,
            )
        )
    ).scalar_one()
    if live_keys >= MAX_ACTIVE_KEYS:
        raise ProblemError(
            409,
            "credential-limit-reached",
            "Conflict",
            "This service account already has the maximum number of active keys.",
        )
    if max_active is not None and live_keys >= max_active:
        raise token_limit_reached(max_active, live_keys)
    owner = await load_folder_facts(session, account.owner_folder_id)
    if owner is None:  # pragma: no cover - the foreign key guarantees the folder
        raise _not_found()
    minted = new_credential()
    row = ApiCredential(
        public_id=minted.public_id,
        secret_hash=minted.secret_hash,
        kind=SERVICE_ACCOUNT_KEY_KIND,
        service_account_id=account.id,
        name=name,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    uow.audit(
        "credential.created",
        "api_credential",
        row.id,
        before=None,
        after=uow.snapshot(row),
        scope_path=owner.ancestor_ids,
    )
    await uow.commit()
    return row, minted.token


async def list_service_account_keys(
    session: AsyncSession, service_account_id: uuid.UUID, settings: IdentitySettings
) -> list[tuple[ApiCredential, str, bool]]:
    """An account's keys oldest first as (row, status, expiring_soon); never any secret material.

    ``expiring_soon`` is true for an active key that expires within
    ``credential_expiry_warning_days``. ``last_used_at`` is the batched value (it can lag by one
    flush interval) and tells an owner which key a pipeline still uses before revoking the other.
    """
    rows = (
        await session.execute(
            select(ApiCredential, _STATUS)
            .where(
                ApiCredential.service_account_id == service_account_id,
                ApiCredential.kind == SERVICE_ACCOUNT_KEY_KIND,
            )
            .order_by(ApiCredential.created_at, ApiCredential.id)
        )
    ).all()
    horizon = datetime.now(UTC) + timedelta(days=settings.credential_expiry_warning_days)
    return [
        (row, str(state), state == "active" and row.expires_at <= horizon) for row, state in rows
    ]


async def revoke_service_account_key(
    uow: UnitOfWork, actor: Principal, service_account_id: uuid.UUID, key_id: uuid.UUID
) -> None:
    """Revoke one key of an account: ``revoked_at = now()``, one ``credential.revoked`` event.

    A key of another account or an unknown id is 404; an already revoked key is left alone and
    writes no second event. Effective on the key's next request (no cache, D-72).
    """
    session = uow.session
    row = (
        await session.execute(
            select(ApiCredential)
            .where(
                ApiCredential.id == key_id,
                ApiCredential.service_account_id == service_account_id,
                ApiCredential.kind == SERVICE_ACCOUNT_KEY_KIND,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found()
    owner_id = (
        await session.execute(
            select(ServiceAccount.owner_folder_id).where(ServiceAccount.id == service_account_id)
        )
    ).scalar_one()
    await require_permission(session, actor, owner_id, "service-account:manage-keys")
    if row.revoked_at is not None:
        return
    owner = await load_folder_facts(session, owner_id)
    if owner is None:  # pragma: no cover - the foreign key guarantees the folder
        raise _not_found()
    before = uow.snapshot(row)
    row.revoked_at = func.now()
    await session.flush()
    await session.refresh(row)
    uow.audit(
        "credential.revoked",
        "api_credential",
        row.id,
        before=before,
        after=uow.snapshot(row),
        scope_path=owner.ancestor_ids,
    )
    await uow.commit()


# One statement per batch. GREATEST keeps last_used_at monotonic even when several identity
# replicas flush interleaved batches. Core UPDATE outside any UnitOfWork: no audit event, and
# updated_at / updated_by stay untouched (last use is telemetry, not a business write).
_FLUSH = text(
    "UPDATE api_credentials SET last_used_at = "
    "GREATEST(COALESCE(api_credentials.last_used_at, v.at), v.at) "
    "FROM unnest(CAST(:ids AS uuid[]), CAST(:ats AS timestamptz[])) AS v(id, at) "
    "WHERE api_credentials.id = v.id"
).bindparams(
    bindparam("ids", type_=ARRAY(PG_UUID(as_uuid=True))),
    bindparam("ats", type_=ARRAY(DateTime(timezone=True))),
)


class CredentialUsageRecorder:
    """Batches ``last_used_at``: ``record`` touches memory only, ``flush`` writes one UPDATE.

    No request ever writes ``last_used_at`` (no per-request write, no row-lock contention). The
    value is best effort: it can lag by up to one flush interval, a crash loses at most one
    interval, and it is never an input to any authorization decision (D-72 liveness is always a
    live database read). At most ``max_pending`` distinct credentials are held between flushes.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, max_pending: int = 10000
    ) -> None:
        self._session_factory = session_factory
        self._max_pending = max_pending
        self._pending: dict[uuid.UUID, datetime] = {}
        self._warned = False

    @property
    def pending(self) -> Mapping[uuid.UUID, datetime]:
        return dict(self._pending)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def record(self, credential_id: uuid.UUID, at: datetime) -> None:
        """Remember the latest use of a credential (synchronous, memory only)."""
        held = self._pending.get(credential_id)
        if held is not None:
            if at > held:
                self._pending[credential_id] = at
            return
        if len(self._pending) >= self._max_pending:
            if not self._warned:
                self._warned = True
                _log.warning("credential_usage_pending_full", max_pending=self._max_pending)
            return
        self._pending[credential_id] = at

    async def flush(self) -> int:
        """Write every pending use in one UPDATE; returns how many credentials it covered.

        A failed flush logs and drops the batch: last-used data is best effort.
        """
        batch, self._pending = self._pending, {}
        self._warned = False
        if not batch:
            return 0
        try:
            async with self._session_factory() as session:
                await session.execute(_FLUSH, {"ids": list(batch), "ats": list(batch.values())})
                await session.commit()
        except Exception as exc:  # best effort by design
            _log.error("credential_usage_flush_failed", error=type(exc).__name__)
            return 0
        return len(batch)

    def lifespan(self, flush_s: float) -> LifespanHook:
        """Lifespan hook: flush every ``flush_s`` seconds and once more on shutdown."""

        @asynccontextmanager
        async def _hook(app: FastAPI) -> AsyncIterator[None]:
            stop = asyncio.Event()

            async def _loop() -> None:
                while not stop.is_set():
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=flush_s)
                    await self.flush()

            task = asyncio.create_task(_loop(), name="credential-usage-flush")
            app.state.credential_usage_task = task
            try:
                yield
            finally:
                stop.set()
                await asyncio.gather(task, return_exceptions=True)
                await self.flush()

        return _hook
