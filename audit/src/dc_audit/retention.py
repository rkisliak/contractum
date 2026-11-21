"""Audit retention (quick 261120-0ye, D-33, D-42): delete events older than the configured days.

The setting is event-carried (D-07): the audit service reads the newest ``settings.updated`` or
``settings.imported`` event of identity that carries the ``platform`` section and takes
``audit_retention_days`` from it. Absent means ``DC_RETENTION_DAYS`` (unset: keep every event),
``0`` means keep for ever, and any other value is raised to the floor ``DC_RETENTION_MIN_DAYS``
(so a forged or mistaken low value never empties the log). The job runs every
``DC_RETENTION_INTERVAL_S`` seconds and deletes at most ``DC_RETENTION_MAX_BATCHES`` batches of
``DC_RETENTION_BATCH`` rows per run (``dc_audit.settings.AuditSettings``).

Each run holds the session advisory lock ``pg_try_advisory_lock(hashtext('dc-audit-retention'))``
on one dedicated autocommit connection (a second replica, or a second concurrent run, skips),
deletes in bounded batches and never deletes the settings history or its own summary rows
(``aggregate_type`` ``platform_settings`` and ``audit_retention``): the job cannot erase its own
configuration. A run that deleted rows inserts one ``audit.retention_applied`` row (counts and the
cutoff only). Trust: only identity can produce ``audit.identity.*`` rows (NATS publish
permissions and ingest's subject check), and the floor bounds any damage.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
import uuid_utils.compat as uuid_compat
from dc_core.actor import system_actor
from dc_core.app import LifespanHook
from fastapi import FastAPI
from sqlalchemy import text

from dc_audit.settings import AuditSettings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import (
        AsyncConnection,
        AsyncEngine,
        AsyncSession,
        async_sessionmaker,
    )

_log = structlog.get_logger("dc_audit.retention")

LOCK_KEY = "dc-audit-retention"
RETENTION_ACTOR = "system:audit-retention"
PROTECTED_AGGREGATES = ("platform_settings", "audit_retention")
SUMMARY_ACTION = "audit.retention_applied"
INITIAL_DELAY_S = 30.0

_NEWEST_SETTING = text(
    """
    SELECT after -> 'platform' -> 'audit_retention_days' AS days
    FROM audit_event
    WHERE service = 'identity'
      AND aggregate_type = 'platform_settings'
      AND action IN ('settings.updated', 'settings.imported')
      AND after ? 'platform'
    ORDER BY occurred_at DESC, id DESC
    LIMIT 1
    """
)

_DELETE_BATCH = text(
    """
    DELETE FROM audit_event
    WHERE id IN (
      SELECT id FROM audit_event
      WHERE occurred_at < :cutoff
        AND aggregate_type <> ALL (:protected)
      ORDER BY occurred_at
      LIMIT :batch
    )
    """
)

_INSERT_SUMMARY = text(
    """
    INSERT INTO audit_event
      (event_id, service, aggregate_type, aggregate_id, action, scope_path, actor, occurred_at,
       after, changes, schema_version, category, created_by, updated_by)
    VALUES
      (:event_id, 'audit', 'audit_retention', 'audit_event', :action, '{}'::uuid[],
       CAST(:actor AS jsonb), :occurred_at, CAST(:after AS jsonb), '[]'::jsonb, 1, 'change',
       :by, :by)
    """
)


@dataclass(frozen=True)
class RetentionResult:
    """What one run did. ``days`` is the effective retention (``None``: keep for ever)."""

    days: int | None
    deleted: int
    batches: int


def effective_days(stored: Any, env_days: int | None, floor_days: int) -> int | None:
    """The retention in force. ``stored`` is the event's value (absent ``None``, ``0`` for ever)."""
    if isinstance(stored, bool) or not (stored is None or isinstance(stored, int)):
        stored = None  # an unusable value is as good as none
    value = env_days if stored is None else stored
    if value is None or value == 0:
        return None
    return max(int(value), floor_days)


class RetentionJob:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: AuditSettings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def stored_days(self) -> Any:
        async with self._session_factory() as session:
            row = (await session.execute(_NEWEST_SETTING)).one_or_none()
        return None if row is None else row.days

    async def _lock(self) -> AsyncConnection | None:
        engine: AsyncEngine | None = self._session_factory.kw.get("bind")
        if engine is None:
            raise RuntimeError("RetentionJob needs a session factory bound to an engine")
        conn = await engine.connect()
        try:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            got = (
                await conn.execute(
                    text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": LOCK_KEY}
                )
            ).scalar_one()
        except BaseException:
            await conn.close()
            raise
        if not got:
            await conn.close()
            return None
        return conn

    @staticmethod
    async def _unlock(conn: AsyncConnection) -> None:
        try:
            # Closing a pooled connection does not end the Postgres session: unlock explicitly.
            await conn.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": LOCK_KEY})
        finally:
            await conn.close()

    async def run_once(self, *, now: datetime | None = None) -> RetentionResult | None:
        """One pass. ``None`` when another run holds the lock; otherwise what was done."""
        settings = self._settings
        conn = await self._lock()
        if conn is None:
            return None
        try:
            days = effective_days(
                await self.stored_days(), settings.retention_days, settings.retention_min_days
            )
            if days is None:
                return RetentionResult(None, 0, 0)
            stamp = (now or datetime.now(UTC)).astimezone(UTC)
            cutoff = stamp - timedelta(days=days)
            deleted = 0
            batches = 0
            while batches < settings.retention_max_batches:
                result = await conn.execute(
                    _DELETE_BATCH,
                    {
                        "cutoff": cutoff,
                        "protected": list(PROTECTED_AGGREGATES),
                        "batch": settings.retention_batch,
                    },
                )
                batches += 1
                count = int(result.rowcount)
                deleted += count
                if count < settings.retention_batch:
                    break
            if deleted:
                await self._summarise(conn, days=days, cutoff=cutoff, deleted=deleted, now=stamp)
            return RetentionResult(days, deleted, batches)
        finally:
            await self._unlock(conn)

    @staticmethod
    async def _summarise(
        conn: AsyncConnection, *, days: int, cutoff: datetime, deleted: int, now: datetime
    ) -> None:
        import json

        actor = system_actor("audit-retention")
        await conn.execute(
            _INSERT_SUMMARY,
            {
                "event_id": uuid_compat.uuid7(),
                "action": SUMMARY_ACTION,
                "actor": json.dumps(
                    {"type": actor.type, "id": actor.id, "display": actor.display, "via": actor.via}
                ),
                "occurred_at": now,
                "after": json.dumps(
                    {
                        "retention_days": days,
                        "cutoff": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "deleted": deleted,
                    }
                ),
                "by": RETENTION_ACTOR,
            },
        )

    async def run(self) -> None:
        """The loop (cancellable): a first pass after a short delay, then every interval."""
        interval = float(self._settings.retention_interval_s)
        await asyncio.sleep(min(interval, INITIAL_DELAY_S))
        while True:
            try:
                result = await self.run_once()
                if result is not None and result.deleted:
                    # Counts only: never an event, actor or scope.
                    _log.info(
                        "audit_retention_applied",
                        days=result.days,
                        deleted=result.deleted,
                        batches=result.batches,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.warning("audit_retention_failed", error=type(exc).__name__)
            await asyncio.sleep(interval)


def retention_hook(settings: AuditSettings) -> LifespanHook:
    """Lifespan hook running the retention loop; ``app.state.retention_job`` for tests."""

    @asynccontextmanager
    async def _hook(app: FastAPI) -> AsyncIterator[None]:
        job = RetentionJob(app.state.session_factory, settings)
        app.state.retention_job = job
        task = asyncio.create_task(job.run(), name="audit-retention")
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return _hook


__all__ = [
    "LOCK_KEY",
    "PROTECTED_AGGREGATES",
    "RetentionJob",
    "RetentionResult",
    "effective_days",
    "retention_hook",
]
