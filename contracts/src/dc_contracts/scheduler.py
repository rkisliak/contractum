"""The sunset-approaching scheduler (SUB-07, D-96).

A deprecated version has a sunset date (a UTC calendar date). Every ``sunset_scan_interval_s`` one
replica scans the active pins on deprecated versions and records a
``contract.version.sunset_approaching`` notice on each pinning card, once per pin per configured
lead time. There is no contract operation to protect here, so the scheduler records notices
directly through ``notices.notify_card``, the same materialiser the NATS consumer uses (D-99): each
new notice gets pending delivery rows for the channels the card has enabled at that moment, and
the delivery worker sends them. No outbox event is written, because nothing audited happened.

Exactly-once per pin and lead comes from the dedupe key
``sunset:<subscription_id>:<lead>:<sunset_date>`` and ``uq_card_notice_card_dedupe``: a repeat scan,
or a second replica scanning at the same moment, conflicts and creates neither a notice nor a
delivery row. The advisory lock only keeps replicas from doing the same work; the constraint is the
guarantee. Re-dating a sunset changes the date in the key, so the pin is warned again.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import structlog
from dc_core.app import LifespanHook
from dc_core.platform_settings import RuntimeSettingsClient
from fastapi import FastAPI
from sqlalchemy import select, text

from dc_contracts.advisory_lock import close_session_lock, try_session_lock
from dc_contracts.models import (
    CardSubscription,
    Contract,
    ContractVersion,
    SubscriberCard,
)
from dc_contracts.notices import notice_payload, notify_card
from dc_contracts.runtime_settings import worker_notifications
from dc_contracts.settings import ContractsSettings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import (
        AsyncConnection,
        AsyncSession,
        async_sessionmaker,
    )

_log = structlog.get_logger("dc_contracts.scheduler")

LOCK_KEY = "dc-card-sunset"
EVENT_TYPE = "contract.version.sunset_approaching"


def pick_lead(days_left: int, leads: Sequence[int]) -> int | None:
    """The smallest configured lead ``L`` with ``days_left <= L`` (inclusive), else ``None``.

    ``None`` too when the sunset has passed (``days_left < 0``): a past sunset is no longer an
    approaching one. Order of ``leads`` does not matter.
    """
    if days_left < 0:
        return None
    qualifying = [lead for lead in leads if days_left <= lead]
    return min(qualifying) if qualifying else None


class SunsetScheduler:
    """Scans for pins nearing a sunset and records the notices (see the module docstring)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: ContractsSettings,
        *,
        settings_client: RuntimeSettingsClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        # Reads the stored lead times (quick 261120-0yd, D-24); None means the env values only.
        self.settings_client = settings_client

    async def scan_once(self, today: date | None = None) -> int:
        """One scan in one transaction; returns the number of notices actually inserted."""
        now = datetime.now(UTC)
        today = today or now.date()
        # Read on every scan (the client caches): a stored change applies from the next scan, and
        # the dedupe key holds lead and sunset date, so a lead already sent is never sent again.
        leads = (await worker_notifications(self.settings_client, self._settings)).sunset_lead_days
        inserted = 0
        async with self._session_factory() as session, session.begin():
            rows = (
                await session.execute(
                    select(
                        CardSubscription.id,
                        CardSubscription.card_id,
                        CardSubscription.contract_id,
                        CardSubscription.version_id,
                        ContractVersion.semver,
                        ContractVersion.sunset_date,
                        Contract.odcs_id,
                    )
                    .join(ContractVersion, ContractVersion.id == CardSubscription.version_id)
                    .join(SubscriberCard, SubscriberCard.id == CardSubscription.card_id)
                    .join(Contract, Contract.id == CardSubscription.contract_id)
                    .where(
                        CardSubscription.ended_at.is_(None),
                        SubscriberCard.archived_at.is_(None),
                        Contract.deleted_at.is_(None),
                        ContractVersion.deprecated_at.is_not(None),
                        ContractVersion.sunset_date >= today,
                    )
                    .order_by(CardSubscription.id)
                )
            ).all()
            for row in rows:
                sunset: date = row.sunset_date
                days_left = (sunset - today).days
                lead = pick_lead(days_left, leads)
                if lead is None:
                    continue
                inserted += await notify_card(
                    session,
                    card_id=row.card_id,
                    event_type=EVENT_TYPE,
                    contract_id=row.contract_id,
                    version_id=row.version_id,
                    payload=notice_payload(
                        contract_id=row.contract_id,
                        odcs_id=str(row.odcs_id),
                        version_id=row.version_id,
                        semver=row.semver,
                        sunset_date=sunset.isoformat(),
                        lead_days=lead,
                        days_left=days_left,
                    ),
                    dedupe_key=f"sunset:{row.id}:{lead}:{sunset.isoformat()}",
                    occurred_at=now,
                )
        return inserted

    # --- the loop ------------------------------------------------------------------------------

    async def _try_lock(self) -> AsyncConnection | None:
        """Session advisory lock on one dedicated connection, held for the life of the loop."""
        return await try_session_lock(self._session_factory, LOCK_KEY, "SunsetScheduler")

    @staticmethod
    async def _close_lock(conn: AsyncConnection | None) -> None:
        await close_session_lock(conn, LOCK_KEY, "sunset_scheduler_unlock_failed")

    async def run(self) -> None:
        """Scan loop (cancellable). Only the replica holding the advisory lock scans."""
        interval = float(self._settings.sunset_scan_interval_s)
        lock_conn: AsyncConnection | None = None
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
                    if lock_conn is not None:
                        created = await self.scan_once()
                        # Counts only: never a card, contact or contract identifier.
                        _log.info("sunset_scan_done", notices=created)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _log.warning("sunset_scan_failed", error=type(exc).__name__)
                await asyncio.sleep(interval)
        finally:
            await self._close_lock(lock_conn)


def scheduler_hook(settings: ContractsSettings) -> LifespanHook:
    """Lifespan hook: ``app.state.sunset_scheduler`` is always set; the loop runs when enabled."""

    @asynccontextmanager
    async def _hook(app: FastAPI) -> AsyncIterator[None]:
        scheduler = SunsetScheduler(
            app.state.session_factory,
            settings,
            settings_client=getattr(app.state, "settings_client", None),
        )
        app.state.sunset_scheduler = scheduler
        task = (
            asyncio.create_task(scheduler.run(), name="sunset-scheduler")
            if settings.sunset_scheduler_enabled
            else None
        )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    return _hook
