"""Opaque rotating refresh tokens with reuse detection (AUTH-01, D-03, D-23).

Raw tokens are ``secrets.token_urlsafe(32)`` (256 bits); only their SHA-256 hex digest is stored.
A token is *live* when it is neither rotated nor revoked and has not expired. Tokens form a
*family*: every rotation issues the next token into the same family, and presenting a token that
was already rotated or revoked kills the whole family (a stolen token cannot be used quietly).

Audit rules (D-23): routine issuance and rotation write no event. Every revocation of durable
session state writes exactly one event with a typed actor and a before/after status:
``session.logged_out`` (logout) and ``session.reuse_detected`` (reuse; committed although the
request then fails with 401). Revoke-all on password rotation never audits here: the rotation's own
single ``user.password_rotated`` event records ``active_sessions N -> 0``. Snapshots hold only
status, user id and reason, never a raw token, its hash or token rows (AUD-03).

Session bookkeeping (quick 261120-s0p, migration 0013): each row also stores ``client_summary``, a
fixed-vocabulary summary of the sign-in User-Agent such as "Chrome on macOS" (at most 64
characters; the raw User-Agent is never stored, logged or audited), and ``access_jti``, the jti of
the access token issued with the row. The caller's own session is the family whose row carries
the caller's token jti. ``list_sessions`` reads the live families for the session list; "last
active" is the ``created_at`` of the family's live row, which is the last sign-in or refresh.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import uuid_utils.compat as uuid_compat
from dc_core.actor import Actor, system_actor
from dc_core.problems import ProblemError
from dc_core.uow import UnitOfWork
from sqlalchemy import ColumnElement, and_, func, select

from dc_identity.models import Folder, OutboxEvent, RefreshToken, User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dc_identity.settings import IdentitySettings

SESSION_SYSTEM_ACTOR = system_actor("session")

_LIVE = and_(
    RefreshToken.rotated_at.is_(None),
    RefreshToken.revoked_at.is_(None),
    RefreshToken.expires_at > func.now(),
)


CLIENT_SUMMARY_MAX = 64

_BROWSERS: tuple[tuple[str, str], ...] = (
    ("Edg/", "Edge"),
    ("EdgA/", "Edge"),
    ("EdgiOS/", "Edge"),
    ("OPR/", "Opera"),
    ("Chrome/", "Chrome"),
    ("CriOS/", "Chrome"),
    ("Firefox/", "Firefox"),
    ("FxiOS/", "Firefox"),
    ("Safari/", "Safari"),
)
_SYSTEMS: tuple[tuple[str, str], ...] = (
    ("Windows", "Windows"),
    ("iPhone", "iOS"),
    ("iPad", "iOS"),
    ("Android", "Android"),
    ("CrOS", "ChromeOS"),
    ("Mac OS X", "macOS"),
    ("Macintosh", "macOS"),
    ("Linux", "Linux"),
)
_API_CLIENTS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"(?:^|[\s(]){re.escape(token)}(?:[/\s;)]|$)", re.IGNORECASE), label)
    for token, label in (
        ("curl", "curl"),
        ("python-httpx", "python-httpx"),
        ("python-requests", "python-requests"),
        ("PowerShell", "PowerShell"),
        ("Go-http-client", "Go-http-client"),
        ("node", "node"),
        ("okhttp", "okhttp"),
    )
)


def summarize_user_agent(raw: str | None) -> str | None:
    """Reduce a User-Agent to a fixed-vocabulary summary, or ``None`` when there is none.

    The result is always one of "<Browser> on <System>", "<Browser>", "API client (<name>)" or
    "Other client", built from the tables above and never from the input, so a planted string
    cannot reach the database, the API or a log (the raw User-Agent is never stored).
    """
    if raw is None or not raw.strip():
        return None
    for pattern, label in _API_CLIENTS:
        if pattern.search(raw):
            return f"API client ({label})"
    browser = next((name for token, name in _BROWSERS if token in raw), None)
    if browser is None:
        return "Other client"
    system = next((name for token, name in _SYSTEMS if token in raw), None)
    summary = browser if system is None else f"{browser} on {system}"
    return summary[:CLIENT_SUMMARY_MAX]


def session_actor(user: User) -> Actor:
    """Audit actor for a session operation performed by the token's own user."""
    return Actor(
        type="user",
        id=f"local:{user.id}",
        display=user.display_name,
        via="refresh_token",
    )


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _invalid_token() -> ProblemError:
    return ProblemError(401, "invalid-credentials", "Unauthorized", "Invalid refresh token.")


def _unit_of_work(
    session_factory: async_sessionmaker[AsyncSession],
    actor: Actor,
    settings: IdentitySettings,
) -> UnitOfWork:
    return UnitOfWork(
        session_factory, actor=actor, outbox_model=OutboxEvent, service=settings.service
    )


async def root_folder_id(session: AsyncSession) -> uuid.UUID:
    return (await session.execute(select(Folder.id).where(Folder.kind == "root"))).scalar_one()


def _snapshot(user_id: uuid.UUID, *, status: str, reason: str | None = None) -> dict[str, str]:
    snap = {"status": status, "user_id": str(user_id)}
    if reason is not None:
        snap["reason"] = reason
    return snap


async def issue_refresh_token(
    session: AsyncSession,
    user: User,
    *,
    family_id: uuid.UUID | None = None,
    ttl_s: int,
    session_started_at: datetime | None = None,
    client_summary: str | None = None,
    access_jti: str | None = None,
) -> str:
    """Add a new live token (a new family unless ``family_id`` is given); returns the raw value.

    ``session_started_at`` is the sign-in the family began with (copied on rotation, D-39); a new
    family starts now. ``client_summary`` is the fixed-vocabulary device summary of the sign-in and
    ``access_jti`` the jti of the access token issued with this row (session list bookkeeping).
    """
    raw = secrets.token_urlsafe(32)
    session.add(
        RefreshToken(
            user_id=user.id,
            family_id=family_id or uuid_compat.uuid7(),
            token_hash=hash_token(raw),
            expires_at=func.now() + timedelta(seconds=ttl_s),
            session_started_at=session_started_at if session_started_at is not None else func.now(),
            client_summary=client_summary,
            access_jti=access_jti,
        )
    )
    return raw


@dataclass(frozen=True)
class Rotated:
    """The result of a rotation: the user, the next raw token and the seconds the session has left
    (``None`` when no absolute cap applies)."""

    user: User
    token: str
    session_remaining_s: int | None


async def _lookup(
    session_factory: async_sessionmaker[AsyncSession], raw: str
) -> tuple[RefreshToken, User] | None:
    """Resolve the token and its user without taking locks; ``None`` for an unknown token."""
    async with session_factory() as session:
        row = (
            await session.execute(
                select(RefreshToken, User)
                .join(User, User.id == RefreshToken.user_id)
                .where(RefreshToken.token_hash == hash_token(raw))
            )
        ).one_or_none()
        if row is None:
            return None
        token, user = row
        return token, user


async def _live_family(
    session: AsyncSession, family_id: uuid.UUID, *, lock: bool = True
) -> list[RefreshToken]:
    stmt = select(RefreshToken).where(RefreshToken.family_id == family_id, _LIVE)
    if lock:
        stmt = stmt.with_for_update()
    return list((await session.execute(stmt)).scalars())


async def rotate_refresh_token(
    session_factory: async_sessionmaker[AsyncSession],
    raw: str,
    *,
    settings: IdentitySettings,
    idle_ttl_s: int | None = None,
    session_max_s: int | None = None,
    access_jti: str | None = None,
) -> Rotated:
    """Exchange a live refresh token for the next one in its family.

    ``idle_ttl_s`` is the effective idle timeout (default ``settings.refresh_token_ttl_s``) and
    ``session_max_s`` the absolute session cap (default ``settings.session_max_s``, D-39): a token
    whose session started longer ago is refused with the expired-token answer, no event.

    Routine rotation writes no audit event (exempt, D-23). Presenting a token that was already
    rotated or revoked while its family still has a live token revokes the family in its own
    committed unit of work with one ``session.reuse_detected`` event, then answers 401.

    ``access_jti`` is the jti of the access token issued with the new row; the device summary is
    copied from the rotated row.
    """
    idle = settings.refresh_token_ttl_s if idle_ttl_s is None else idle_ttl_s
    cap = settings.session_max_s if session_max_s is None else session_max_s
    found = await _lookup(session_factory, raw)
    if found is None:
        raise _invalid_token()
    looked_up, user = found
    if not user.is_active:
        raise _invalid_token()
    # Session cutoff (D-75): a refresh token created at or before the reactivation never works
    # again, whether or not it escaped the deactivation's revocation. No event, family untouched.
    if user.sessions_valid_after is not None and looked_up.created_at <= user.sessions_valid_after:
        raise _invalid_token()

    reuse_family: uuid.UUID | None = None
    uow = _unit_of_work(session_factory, session_actor(user), settings)
    async with uow:
        token = (
            await uow.session.execute(
                select(RefreshToken)
                .where(RefreshToken.id == looked_up.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        used = token.rotated_at is not None or token.revoked_at is not None
        if used:
            if await _live_family(uow.session, token.family_id, lock=False):
                reuse_family = token.family_id
            else:
                raise _invalid_token()
        else:
            live = (
                await uow.session.execute(
                    select(func.count()).where(RefreshToken.id == token.id, _LIVE)
                )
            ).scalar_one()
            if not live:  # expired
                raise _invalid_token()
            # Absolute session cap (D-39), judged on the database clock like every expiry here.
            remaining = (
                await uow.session.execute(
                    select(
                        func.extract(
                            "epoch",
                            RefreshToken.session_started_at + timedelta(seconds=cap) - func.now(),
                        )
                    ).where(RefreshToken.id == token.id)
                )
            ).scalar_one()
            if float(remaining) <= 0:
                raise _invalid_token()
            token.rotated_at = func.now()
            new_raw = await issue_refresh_token(
                uow.session,
                user,
                family_id=token.family_id,
                ttl_s=idle,
                session_started_at=token.session_started_at,
                client_summary=token.client_summary,
                access_jti=access_jti,
            )
            await uow.commit()
            return Rotated(user, new_raw, int(float(remaining)))

    # Reuse: the first unit of work is closed; revoke the family and audit it in a new one that
    # commits before the 401 is raised.
    assert reuse_family is not None  # noqa: S101 - only reachable through the reuse branch
    await _revoke_family(
        session_factory,
        reuse_family,
        user.id,
        actor=SESSION_SYSTEM_ACTOR,
        action="session.reuse_detected",
        reason="reuse_detected",
        settings=settings,
    )
    raise _invalid_token()


async def _revoke_family(
    session_factory: async_sessionmaker[AsyncSession],
    family_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    actor: Actor,
    action: str,
    reason: str,
    settings: IdentitySettings,
) -> bool:
    uow = _unit_of_work(session_factory, actor, settings)
    async with uow:
        live = await _live_family(uow.session, family_id)
        if not live:
            return False
        for row in live:
            row.revoked_at = func.now()
        uow.audit(
            action,
            "session",
            family_id,
            before=_snapshot(user_id, status="active"),
            after=_snapshot(user_id, status="revoked", reason=reason),
            scope_path=[await root_folder_id(uow.session)],
        )
        await uow.commit()
        return True


async def revoke_family_for_token(
    session_factory: async_sessionmaker[AsyncSession],
    raw: str,
    *,
    settings: IdentitySettings,
) -> bool:
    """Logout: revoke the live tokens of the presented token's family, auditing it once.

    An unknown token, or a family with nothing live, writes nothing and returns ``False``.
    """
    found = await _lookup(session_factory, raw)
    if found is None:
        return False
    token, user = found
    return await _revoke_family(
        session_factory,
        token.family_id,
        user.id,
        actor=session_actor(user),
        action="session.logged_out",
        reason="logout",
        settings=settings,
    )


async def revoke_all_for_user(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Revoke every unrevoked token of a user inside the caller's unit of work.

    Returns how many of them were live. Never audits: the caller's single event records the count.
    """
    rows = (
        await session.execute(
            select(RefreshToken, _LIVE.label("live"))
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .with_for_update()
        )
    ).all()
    live_count = 0
    for token, live in rows:
        token.revoked_at = func.now()
        live_count += 1 if live else 0
    await session.flush()
    return live_count


SESSION_LIST_MAX = 100


@dataclass(frozen=True)
class LiveSession:
    """One live family as the session list shows it (ids only for the family, never a token)."""

    family_id: uuid.UUID
    started_at: datetime
    last_active_at: datetime
    idle_expires_at: datetime
    expires_at: datetime
    client_summary: str | None


@dataclass(frozen=True)
class SessionList:
    sessions: list[LiveSession]
    total: int
    current_family_id: uuid.UUID | None


def _live_within_cap(session_max_s: int) -> ColumnElement[bool]:
    """A live row whose session also started inside the absolute cap (the list's liveness rule)."""
    return and_(
        _LIVE,
        RefreshToken.session_started_at + timedelta(seconds=session_max_s) > func.now(),
    )


async def family_of_jti(
    session: AsyncSession, user_id: uuid.UUID, current_jti: str | None
) -> uuid.UUID | None:
    """The family of the row (in any state) that carries the caller's access-token jti."""
    if current_jti is None:
        return None
    return (
        await session.execute(
            select(RefreshToken.family_id)
            .where(RefreshToken.user_id == user_id, RefreshToken.access_jti == current_jti)
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_sessions(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    session_max_s: int,
    current_jti: str | None,
) -> SessionList:
    """The user's live sessions, newest activity first (at most ``SESSION_LIST_MAX`` shown).

    A family is live when its newest row is neither rotated nor revoked, has not idled out and its
    session started inside ``session_max_s``. The current family is found from the row (in any
    state) that carries ``current_jti``; it is only marked when it is itself live.
    """
    rows = (
        (
            await session.execute(
                select(RefreshToken)
                .where(RefreshToken.user_id == user_id, _live_within_cap(session_max_s))
                .order_by(RefreshToken.created_at.desc(), RefreshToken.id.desc())
            )
        )
        .scalars()
        .all()
    )
    seen: set[uuid.UUID] = set()
    live: list[LiveSession] = []
    for row in rows:
        if row.family_id in seen:
            continue
        seen.add(row.family_id)
        live.append(
            LiveSession(
                family_id=row.family_id,
                started_at=row.session_started_at,
                last_active_at=row.created_at,
                idle_expires_at=row.expires_at,
                expires_at=row.session_started_at + timedelta(seconds=session_max_s),
                client_summary=row.client_summary,
            )
        )
    current = await family_of_jti(session, user_id, current_jti)
    return SessionList(sessions=live[:SESSION_LIST_MAX], total=len(live), current_family_id=current)


# --- ending sessions (quick 261120-s0p) -----------------------------------------------------------

RevokeOutcome = Literal["revoked", "ended", "unknown"]


async def _live_families(
    session: AsyncSession, user_id: uuid.UUID, *, session_max_s: int | None = None
) -> list[tuple[uuid.UUID, list[RefreshToken]]]:
    """The user's live families with their live rows locked ``FOR UPDATE``, oldest session first.

    ``session_max_s`` additionally requires the session to have started inside the absolute cap
    (the list's rule, used to count sessions against the limit); without it every unexpired,
    unrotated, unrevoked row counts, which is what an explicit revocation may end.
    """
    liveness = _LIVE if session_max_s is None else _live_within_cap(session_max_s)
    rows = list(
        (
            await session.execute(
                select(RefreshToken)
                .where(RefreshToken.user_id == user_id, liveness)
                .order_by(RefreshToken.session_started_at, RefreshToken.id)
                .with_for_update()
            )
        ).scalars()
    )
    grouped: dict[uuid.UUID, list[RefreshToken]] = {}
    for row in rows:
        grouped.setdefault(row.family_id, []).append(row)
    return list(grouped.items())


async def revoke_session(
    uow: UnitOfWork, user_id: uuid.UUID, family_id: uuid.UUID, *, reason: str
) -> RevokeOutcome:
    """End one session of a user: every live row of the family is revoked with one
    ``session.revoked`` event. ``ended`` is a family of this user that has nothing live left (no
    event); ``unknown`` is a family that is not this user's (or does not exist)."""
    session = uow.session
    rows = list(
        (
            await session.execute(
                select(RefreshToken)
                .where(
                    RefreshToken.user_id == user_id,
                    RefreshToken.family_id == family_id,
                    _LIVE,
                )
                .with_for_update()
            )
        ).scalars()
    )
    if not rows:
        known = (
            await session.execute(
                select(RefreshToken.id)
                .where(RefreshToken.user_id == user_id, RefreshToken.family_id == family_id)
                .limit(1)
            )
        ).first()
        return "ended" if known is not None else "unknown"
    for row in rows:
        row.revoked_at = func.now()
    uow.audit(
        "session.revoked",
        "session",
        family_id,
        before=_snapshot(user_id, status="active"),
        after=_snapshot(user_id, status="revoked", reason=reason),
        scope_path=[await root_folder_id(session)],
    )
    return "revoked"


async def revoke_families(
    uow: UnitOfWork,
    user_id: uuid.UUID,
    *,
    keep_family_id: uuid.UUID | None,
    action: str,
    reason: str,
    session_max_s: int | None = None,
    keep_newest: int = 0,
    after_extra: dict[str, Any] | None = None,
) -> list[uuid.UUID]:
    """Revoke the user's live families except ``keep_family_id``, oldest first, with one event.

    ``keep_newest`` additionally keeps that many of the newest remaining families (the session cap
    ends the oldest ones). The event ``action`` (aggregate ``user``) records the live count before
    and after, the reason and the revoked family ids (never a token, hash, jti or User-Agent).
    Nothing is written when nothing is revoked.
    """
    session = uow.session
    families = await _live_families(session, user_id, session_max_s=session_max_s)
    candidates = [fam for fam in families if fam[0] != keep_family_id]
    if keep_newest > 0:
        candidates = candidates[: max(0, len(candidates) - keep_newest)]
    if not candidates:
        return []
    for _, rows in candidates:
        for row in rows:
            row.revoked_at = func.now()
    revoked_ids = [family_id for family_id, _ in candidates]
    after: dict[str, Any] = {
        "active_sessions": len(families) - len(candidates),
        "reason": reason,
        **(after_extra or {}),
        "revoked_session_ids": [str(i) for i in revoked_ids],
    }
    uow.audit(
        action,
        "user",
        user_id,
        before={"active_sessions": len(families)},
        after=after,
        scope_path=[await root_folder_id(session)],
    )
    return revoked_ids


async def revoke_other_sessions(
    uow: UnitOfWork, user_id: uuid.UUID, *, keep_family_id: uuid.UUID, reason: str
) -> list[uuid.UUID]:
    """Sign out every session except ``keep_family_id`` (one ``user.sessions_revoked``)."""
    return await revoke_families(
        uow,
        user_id,
        keep_family_id=keep_family_id,
        action="user.sessions_revoked",
        reason=reason,
    )


async def revoke_all_sessions(
    uow: UnitOfWork, user_id: uuid.UUID, *, reason: str
) -> list[uuid.UUID]:
    """Sign out every session of the user (one ``user.sessions_revoked``)."""
    return await revoke_families(
        uow,
        user_id,
        keep_family_id=None,
        action="user.sessions_revoked",
        reason=reason,
    )


async def enforce_session_limit(
    uow: UnitOfWork, user_id: uuid.UUID, *, limit: int, session_max_s: int
) -> list[uuid.UUID]:
    """Make room for one more session: end the user's oldest live sessions so at most
    ``limit - 1`` remain (quick 261120-s0p, D-09).

    The user row is locked ``FOR UPDATE`` first, so two concurrent sign-ins of one user are counted
    one after the other and can never leave more than ``limit`` live sessions. Sessions are ranked
    by ``session_started_at``, oldest first, with the same liveness rule as the session list. One
    ``user.sessions_evicted`` event (actor: the user signing in) when anything was ended, none
    otherwise. Never refuses the sign-in.
    """
    await uow.session.execute(select(User.id).where(User.id == user_id).with_for_update())
    return await revoke_families(
        uow,
        user_id,
        keep_family_id=None,
        action="user.sessions_evicted",
        reason="session_limit",
        session_max_s=session_max_s,
        keep_newest=limit - 1,
        after_extra={"limit": limit},
    )
