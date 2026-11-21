"""The card notice delivery worker (SUB-07, D-98, D-99).

One worker per deployment: replicas race for the session advisory lock
``hashtext('dc-card-notifier')`` and only the holder delivers (the same shape as the outbox relay).
A pass has three steps and no row lock is ever held across HTTP I/O (research Pattern 9):

1. *claim*: in one short transaction, due ``pending`` deliveries are selected with
   ``FOR UPDATE SKIP LOCKED`` and leased (``next_attempt_at = now() + notify_lease_s``), so a
   crashed worker's rows come back after the lease and two workers never send the same row;
2. *send*: each claimed delivery is sent outside any transaction;
3. *record*: the result is written in its own short transaction.

Delivery never runs inside a contract operation: lifecycle writes only write their outbox event, and
a failing receiver costs nothing but a retry. The worker never touches a card's channel flag: a
failed delivery is marked ``failed`` for that delivery only, and the next event is delivered again
(D-99). ``last_error`` is always a code from a fixed vocabulary, never a body, URL or secret.

Quick 261120-0yd: the retry schedule, the attempt cap and the email templates are the stored
notification settings over the env defaults, read once per pass. A webhook host outside the
allowed-hosts list ends the delivery ``failed`` at once (``webhook-host-not-allowed``); quick
261120-s0p made the list mandatory, so an empty list (and a worker with no notifier key, which
cannot read it) allows no host. When the list cannot be read, or the one-time seed of the list
from the existing channels is not confirmed yet, the delivery is deferred a minute without using
an attempt.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

import httpx
import structlog
from cryptography.fernet import InvalidToken
from dc_core.app import LifespanHook
from dc_core.http import make_client
from dc_core.platform_settings import RuntimeSettingsClient, webhook_host_allowed
from fastapi import FastAPI
from sqlalchemy import func, select, text, update

from dc_contracts.advisory_lock import close_session_lock, try_session_lock
from dc_contracts.mailer import render_email, send_email
from dc_contracts.models import (
    CardNotice,
    CardNoticeDelivery,
    CardWebhookSecret,
    SubscriberCard,
)
from dc_contracts.runtime_settings import EffectiveNotifications, worker_notifications
from dc_contracts.settings import ContractsSettings
from dc_contracts.webhook_host_seed import WebhookHostSeed
from dc_contracts.webhooks import (
    DeliveryOutcome,
    InvalidWebhookUrl,
    SecretBox,
    build_body,
    check_resolved_host,
    send_webhook,
    validate_webhook_url,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import (
        AsyncConnection,
        AsyncSession,
        async_sessionmaker,
    )

_log = structlog.get_logger("dc_contracts.delivery")

LOCK_KEY = "dc-card-notifier"
# How many deliveries of one pass are in flight at once: a slow receiver must not hold up the rest.
SEND_CONCURRENCY = 8
# How many of a host's resolved addresses one attempt may try on connect errors. Bounds the time a
# claim can take against the lease.
MAX_ADDRESS_TRIES = 4
# A delivery that cannot be judged against the allowed-hosts list (identity never answered) is
# looked at again this many seconds later, without using up an attempt (D-25).
UNKNOWN_HOSTS_RETRY_S = 60


def retry_delay(
    attempts: int,
    schedule: Sequence[int],
    max_attempts: int,
    *,
    rand: Callable[[], float] = random.random,
) -> float | None:
    """Seconds to wait after the ``attempts``-th failed attempt, or ``None`` when none are left.

    ``schedule[attempts - 1]`` is the base wait after attempt ``attempts``; an index past the end
    reuses the last entry. The wait is jittered by +-10% (``0.9 + 0.2 * rand()``) so receivers that
    failed together are not all retried in the same second.
    """
    if attempts >= max_attempts:
        return None
    index = min(max(attempts, 1) - 1, len(schedule) - 1)
    return schedule[index] * (0.9 + 0.2 * rand())


@dataclass(frozen=True)
class _Claim:
    """A leased delivery with everything the send step needs, loaded in the claim transaction."""

    delivery_id: uuid.UUID
    channel: str
    attempts: int
    notice: CardNotice
    card_name: str
    channel_enabled: bool
    webhook_url: str | None
    secret_enc: str | None
    previous_enc: str | None
    previous_valid_until: datetime | None
    email_channel_on: bool
    contacts: list[dict[str, Any]]
    card_archived: bool


@dataclass(frozen=True)
class _Result:
    # ``deferred``: not judged yet, looked at again later with no attempt used. ``terminal``: a
    # failure that no retry can fix (the delivery ends ``failed`` now).
    kind: Literal["delivered", "skipped", "failed", "deferred"]
    status: int | None = None
    error: str | None = None
    terminal: bool = False


class CardNotifier:
    """Claims due deliveries, sends them and records the result (see the module docstring)."""

    # The channels this worker delivers. An email delivery is handed to the mail service (D-105).
    channels: frozenset[str] = frozenset({"webhook", "email"})

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: ContractsSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
        settings_client: RuntimeSettingsClient | None = None,
        host_seed: WebhookHostSeed | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        # Quick 261120-s0p: until the webhook allow-list seed is confirmed, webhook deliveries
        # are deferred. None (a worker built without the seed hook) means nothing to wait for.
        self.host_seed = host_seed
        # Reads the stored notification settings (D-22); None means the env values only.
        self.settings_client = settings_client
        self._owns_client = http_client is None
        self._client = http_client or make_client(timeout_s=settings.webhook_timeout_s)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- claim ---------------------------------------------------------------------------------

    async def claim_due(self) -> list[_Claim]:
        """Lease up to ``notify_batch_size`` due deliveries in one short transaction."""
        settings = self._settings
        async with self._session_factory() as session, session.begin():
            ids = (
                (
                    await session.execute(
                        select(CardNoticeDelivery.id)
                        .where(
                            CardNoticeDelivery.status == "pending",
                            CardNoticeDelivery.next_attempt_at <= func.now(),
                            CardNoticeDelivery.channel.in_(sorted(self.channels)),
                        )
                        .order_by(CardNoticeDelivery.next_attempt_at)
                        .limit(settings.notify_batch_size)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            if not ids:
                return []
            await session.execute(
                update(CardNoticeDelivery)
                .where(CardNoticeDelivery.id.in_(ids))
                .values(next_attempt_at=func.now() + timedelta(seconds=settings.notify_lease_s))
            )
            rows = await session.execute(
                select(
                    CardNoticeDelivery.id,
                    CardNoticeDelivery.channel,
                    CardNoticeDelivery.attempts,
                    CardNotice,
                    SubscriberCard.name,
                    SubscriberCard.webhook_enabled,
                    SubscriberCard.webhook_url,
                    CardWebhookSecret.secret_enc,
                    CardWebhookSecret.previous_enc,
                    CardWebhookSecret.previous_valid_until,
                    SubscriberCard.email_enabled,
                    SubscriberCard.contacts,
                    SubscriberCard.archived_at.is_not(None),
                )
                .join(CardNotice, CardNotice.id == CardNoticeDelivery.notice_id)
                .join(SubscriberCard, SubscriberCard.id == CardNotice.card_id)
                .outerjoin(CardWebhookSecret, CardWebhookSecret.card_id == SubscriberCard.id)
                .where(CardNoticeDelivery.id.in_(ids))
                .order_by(CardNoticeDelivery.id)
                .execution_options(populate_existing=True)
            )
            claims = [
                _Claim(
                    delivery_id=row[0],
                    channel=row[1],
                    attempts=row[2],
                    notice=row[3],
                    card_name=row[4],
                    channel_enabled=row[5],
                    webhook_url=row[6],
                    secret_enc=row[7],
                    previous_enc=row[8],
                    previous_valid_until=row[9],
                    email_channel_on=row[10],
                    contacts=list(row[11] or []),
                    card_archived=bool(row[12]),
                )
                for row in rows.all()
            ]
            session.expunge_all()
            return claims

    # --- send ----------------------------------------------------------------------------------

    def _signing_secrets(self, claim: _Claim) -> list[str] | None:
        """The current secret, then the previous one while its overlap window is open."""
        key = self._settings.webhook_secret_key
        if key is None or claim.secret_enc is None:
            return None
        box = SecretBox(key)
        try:
            secrets_ = [box.open(claim.secret_enc)]
            if (
                claim.previous_enc is not None
                and claim.previous_valid_until is not None
                and claim.previous_valid_until > datetime.now(UTC)
            ):
                secrets_.append(box.open(claim.previous_enc))
        except InvalidToken:
            return None
        return secrets_

    async def _send_webhook(self, claim: _Claim, effective: EffectiveNotifications) -> _Result:
        if not claim.channel_enabled or not claim.webhook_url:
            return _Result("skipped", error="channel-disabled")
        if not effective.allowed_hosts_known:
            # The allowed-hosts list could not be read, or its one-time seed is not confirmed
            # yet: fail closed, look again later (D-25).
            return _Result("deferred", error="settings-unavailable")
        signing = self._signing_secrets(claim)
        if signing is None:
            return _Result("failed", error="no-key")
        try:
            target = validate_webhook_url(claim.webhook_url, self._settings)
        except InvalidWebhookUrl:
            return _Result("failed", error="invalid-url")
        if not webhook_host_allowed(target.host, effective.allowed_hosts):
            # The list is mandatory (empty allows nothing) and every check below still applies
            # (D-25). No retry can help.
            return _Result("failed", error="webhook-host-not-allowed", terminal=True)
        # Send-time SSRF guard (T-04.1-28, T-04.1-29): DNS may have changed since the URL was
        # accepted, so resolve again and refuse unless every address is globally routable. The
        # request then connects to a validated address, not to the name. An allow-listed host
        # (development sink) is exempt from the check and connects by name.
        addresses: list[str | None] = [None]
        if target.host not in self._settings.webhook_allowed_private_hosts:
            try:
                addresses = list(
                    (await check_resolved_host(target.host, target.port, self._settings))[
                        :MAX_ADDRESS_TRIES
                    ]
                )
            except InvalidWebhookUrl as exc:
                return _Result("failed", error=exc.code)
        body = build_body(claim.notice, claim.card_name)
        # Every address is validated, so the next one is as safe as the first. A connect error
        # means nothing was sent (a dual-stack receiver whose IPv6 address this network cannot
        # reach), so try the next address; any other outcome is the receiver's answer and final.
        outcome: DeliveryOutcome
        for address in addresses:
            outcome = await send_webhook(
                self._client,
                target,
                signing,
                claim.delivery_id,
                body,
                self._settings,
                address,
            )
            if outcome.ok or outcome.error != "connect-error":
                break
        if outcome.ok:
            return _Result("delivered", status=outcome.status)
        return _Result("failed", status=outcome.status, error=outcome.error)

    async def _send_email(self, claim: _Claim, effective: EffectiveNotifications) -> _Result:
        """Hand the notice to the mail service for the card's email contacts as they are now."""
        if not claim.email_channel_on:
            return _Result("skipped", error="channel-disabled")
        recipients = list(
            dict.fromkeys(
                str(contact["value"])
                for contact in claim.contacts
                if contact.get("kind") == "email" and contact.get("value")
            )
        )
        if not recipients:
            return _Result("skipped", error="no-recipients")
        settings = self._settings
        if not settings.mail_url or settings.mail_api_key is None:
            return _Result("failed", error="mail-not-configured")
        subject, text = render_email(claim.notice, claim.card_name, effective.templates)
        outcome = await send_email(self._client, settings, recipients, subject, text)
        if outcome.ok:
            # A partial refusal is still delivered: the accepted recipients must not get a copy
            # again. The count is kept in last_error so the card shows it.
            error = f"mail-recipients-refused:{outcome.refused}" if outcome.refused else None
            return _Result("delivered", status=outcome.status, error=error)
        return _Result("failed", status=outcome.status, error=outcome.error)

    async def _attempt(self, claim: _Claim, effective: EffectiveNotifications) -> _Result:
        if claim.card_archived:
            # Archiving is the owner's way to stop notifications (D-93): a notice queued, or
            # waiting out a retry, before the archive is not sent after it.
            return _Result("skipped", error="card-archived")
        if claim.channel == "webhook":
            return await self._send_webhook(claim, effective)
        if claim.channel == "email":
            return await self._send_email(claim, effective)
        raise ValueError(f"no sender for channel {claim.channel!r}")  # unreachable: claim filters

    # --- record --------------------------------------------------------------------------------

    async def _record(
        self, claim: _Claim, result: _Result, effective: EffectiveNotifications
    ) -> None:
        attempts = claim.attempts + 1
        values: dict[str, object]
        if result.kind == "delivered":
            values = {
                "status": "delivered",
                "attempts": attempts,
                "last_attempt_at": func.now(),
                "last_status": result.status,
                "last_error": result.error,
                "delivered_at": func.now(),
            }
        elif result.kind == "skipped":
            values = {"status": "skipped", "last_error": result.error}
        elif result.kind == "deferred":
            # Not judged yet: no attempt is used, the row comes back a minute later.
            values = {
                "last_error": result.error,
                "next_attempt_at": func.now() + timedelta(seconds=UNKNOWN_HOSTS_RETRY_S),
            }
        else:
            values = {
                "attempts": attempts,
                "last_attempt_at": func.now(),
                "last_status": result.status,
                "last_error": result.error,
            }
            delay = (
                None
                if result.terminal
                else retry_delay(attempts, effective.retry_schedule_s, effective.max_attempts)
            )
            if delay is None:
                values["status"] = "failed"
            else:
                values["next_attempt_at"] = func.now() + timedelta(seconds=delay)
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(CardNoticeDelivery)
                .where(
                    CardNoticeDelivery.id == claim.delivery_id,
                    CardNoticeDelivery.status == "pending",
                )
                .values(**values)
            )
        # Identifiers, channel, status and error code only: never a URL, body or secret.
        _log.info(
            "card_notice_delivery",
            delivery_id=str(claim.delivery_id),
            channel=claim.channel,
            result=result.kind,
            attempts=attempts,
            http_status=result.status,
            error=result.error,
        )

    async def _process(
        self, claim: _Claim, gate: asyncio.Semaphore, effective: EffectiveNotifications
    ) -> None:
        async with gate:
            try:
                result = await self._attempt(claim, effective)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A send-step error is deterministic (a bad secret, an encoding bug): count it as a
                # failed attempt so the retry schedule and the terminal state apply, instead of
                # leaving the row to be re-claimed for ever with no cause on the card.
                _log.warning(
                    "card_notice_delivery_error",
                    delivery_id=str(claim.delivery_id),
                    error=type(exc).__name__,
                )
                result = _Result("failed", error="internal-error")
            try:
                await self._record(claim, result, effective)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A record-step error is transient (the database): the row stays leased and comes
                # back after the lease; log the class only.
                _log.warning(
                    "card_notice_record_error",
                    delivery_id=str(claim.delivery_id),
                    error=type(exc).__name__,
                )

    async def run_once(self) -> int:
        """One claim, send and record pass; returns how many deliveries it handled."""
        claims = await self.claim_due()
        if not claims:
            return 0
        # Read the seed state BEFORE the settings: the seed task drops the cached list and only
        # then flips ``done``, so a pass that sees ``done`` also reads the seeded list.
        seed_done = self.host_seed is None or self.host_seed.done
        # Read once per batch (the client caches): a stored change applies from the next pass.
        effective = await worker_notifications(self.settings_client, self._settings)
        if not seed_done:
            # The allow-list is not seeded yet: the webhook branch defers (no attempt used).
            effective = replace(effective, allowed_hosts_known=False)
        gate = asyncio.Semaphore(SEND_CONCURRENCY)
        await asyncio.gather(*(self._process(claim, gate, effective) for claim in claims))
        return len(claims)

    # --- loop ----------------------------------------------------------------------------------

    async def _try_lock(self) -> AsyncConnection | None:
        """Session advisory lock on one dedicated connection, held for the life of the loop."""
        return await try_session_lock(self._session_factory, LOCK_KEY, "CardNotifier")

    @staticmethod
    async def _close_lock(conn: AsyncConnection | None) -> None:
        await close_session_lock(conn, LOCK_KEY, "card_notifier_unlock_failed")

    async def run(self) -> None:
        """Delivery loop (cancellable). Only the replica holding the advisory lock delivers."""
        poll = self._settings.notify_poll_interval_s
        lock_conn: AsyncConnection | None = None
        backoff = poll
        try:
            while True:
                try:
                    if lock_conn is not None:
                        # A dead lock connection means the lock is gone: reacquire.
                        try:
                            await lock_conn.execute(text("SELECT 1"))
                        except Exception:
                            await self._close_lock(lock_conn)
                            lock_conn = None
                    if lock_conn is None:
                        lock_conn = await self._try_lock()
                        if lock_conn is None:
                            await asyncio.sleep(poll)
                            continue
                    handled = await self.run_once()
                    backoff = poll
                    if handled < self._settings.notify_batch_size:
                        await asyncio.sleep(poll)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _log.warning("card_notifier_loop_error", error=type(exc).__name__)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
        finally:
            await self._close_lock(lock_conn)


def notifier_hook(settings: ContractsSettings) -> LifespanHook:
    """Lifespan hook: ``app.state.card_notifier`` is always set; the loop runs only when enabled."""

    @asynccontextmanager
    async def _hook(app: FastAPI) -> AsyncIterator[None]:
        client = make_client(timeout_s=settings.webhook_timeout_s)
        notifier = CardNotifier(
            app.state.session_factory,
            settings,
            http_client=client,
            settings_client=getattr(app.state, "settings_client", None),
            host_seed=getattr(app.state, "webhook_host_seed", None),
        )
        app.state.card_notifier = notifier
        # The same client carries the Settings test email to the mail service.
        app.state.outbound_http = client
        task = (
            asyncio.create_task(notifier.run(), name="card-notifier")
            if settings.notify_worker_enabled
            else None
        )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.aclose()

    return _hook
