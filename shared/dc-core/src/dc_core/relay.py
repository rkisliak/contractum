"""Outbox relay: drains a service's ``outbox`` table into the event transport (AUD-02).

The relay talks only to the :class:`~dc_core.events.EventPublisher` it is given (D-21) and never
imports a NATS module. Delivery is at-least-once: a row stays unpublished until the publisher
acknowledges it, and a crash between publish and commit republishes the row, which the transport
(``Nats-Msg-Id``) or the audit store (unique ``event_id``) absorbs.

Rows are claimed with ``FOR UPDATE SKIP LOCKED`` ordered by id. No high-water-mark cursor is kept:
a lower id can commit after a higher id was already published, and it is still picked up.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func, select, text, update

from dc_core.events import EventPublisher, build_envelope, encode_envelope, subject_for

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import (
        AsyncConnection,
        AsyncEngine,
        AsyncSession,
        async_sessionmaker,
    )

_log = structlog.get_logger("dc_core.relay")


class OutboxRelay:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        outbox_model: Any,
        *,
        service: str,
        publisher: EventPublisher,
        batch: int = 100,
        poll_s: float = 0.5,
        max_backoff_s: float = 30.0,
    ) -> None:
        self._session_factory = session_factory
        self._outbox = outbox_model
        self._service = service
        self._publisher = publisher
        self._batch = batch
        self._poll_s = poll_s
        self._max_backoff_s = max_backoff_s

    async def drain_once(self) -> int:
        """Publish up to ``batch`` unpublished rows in id order; returns how many succeeded.

        On the first failure the successes so far are committed, the failed row's ``attempts`` is
        incremented in its own transaction and the failure is re-raised to the caller.
        """
        outbox: Any = self._outbox
        published = 0
        failure: Exception | None = None
        failed_id: int | None = None
        failed_event_id = ""
        failed_attempts = 0
        async with self._session_factory() as session, session.begin():
            rows = (
                (
                    await session.execute(
                        select(outbox)
                        .where(outbox.published_at.is_(None))
                        .order_by(outbox.id)
                        .limit(self._batch)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            done_ids: list[int] = []
            for row in rows:
                try:
                    subject = subject_for(self._service, row.aggregate_type, row.action)
                    payload = encode_envelope(build_envelope(row, service=self._service))
                    # A duplicate ack (JetStream already has this Nats-Msg-Id) is success too.
                    await self._publisher.publish(subject, payload, msg_id=str(row.event_id))
                except Exception as exc:
                    failure = exc
                    failed_id = row.id
                    failed_event_id = str(row.event_id)
                    failed_attempts = row.attempts + 1
                    break
                done_ids.append(row.id)
            if done_ids:
                await session.execute(
                    update(outbox).where(outbox.id.in_(done_ids)).values(published_at=func.now())
                )
                published = len(done_ids)
        if failure is not None and failed_id is not None:
            async with self._session_factory() as session, session.begin():
                await session.execute(
                    update(outbox)
                    .where(outbox.id == failed_id)
                    .values(attempts=outbox.attempts + 1)
                )
            # No payload in the log: only identifiers and the error class.
            _log.warning(
                "outbox_relay_publish_failed",
                event_id=failed_event_id,
                attempts=failed_attempts,
                error=type(failure).__name__,
            )
            raise failure
        return published

    async def _try_lock(self) -> AsyncConnection | None:
        """Session-level advisory lock on one dedicated connection, held for the relay's life."""
        engine: AsyncEngine | None = self._session_factory.kw.get("bind")
        if engine is None:
            raise RuntimeError("OutboxRelay needs a session factory bound to an engine")
        conn = await engine.connect()
        try:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            got = (
                await conn.execute(
                    text("SELECT pg_try_advisory_lock(hashtext('dc-outbox-relay-' || :service))"),
                    {"service": self._service},
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
    async def _close_lock(conn: AsyncConnection | None) -> None:
        if conn is None:
            return
        try:
            # Invalidate, never only close: close() hands the DBAPI connection back to the pool
            # with the session lock still held, so no replica (this one included, on another
            # pooled connection) could lock again. Invalidating ends the server session, which
            # releases the lock.
            await conn.invalidate()
            await conn.close()
        except Exception as exc:
            _log.warning("outbox_relay_unlock_failed", error=type(exc).__name__)

    async def run(self) -> None:
        """Relay loop (cancellable). Only the replica holding the advisory lock relays."""
        lock_conn: AsyncConnection | None = None
        backoff = self._poll_s
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
                            await asyncio.sleep(self._poll_s)
                            continue
                    count = await self.drain_once()
                    backoff = self._poll_s
                    if count < self._batch:
                        await asyncio.sleep(self._poll_s)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # drain_once already logged a failed row; keep the loop alive and back off.
                    _log.warning(
                        "outbox_relay_loop_error", error=type(exc).__name__, backoff_s=backoff
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self._max_backoff_s)
        finally:
            await self._close_lock(lock_conn)
