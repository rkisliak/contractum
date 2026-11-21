"""Best-effort access events (AUD-04, D-171, D-185): a bounded in-process queue and its flusher.

A route that returns contract content calls :meth:`AccessLog.record` and nothing else. ``record``
is synchronous and O(1): it appends to a ``deque(maxlen=capacity)`` and never awaits, so a broker
outage, a slow publisher or a burst of reads can never block, slow or fail a read. A flusher task
publishes the queue in batches through an :class:`~dc_core.events.EventPublisher` (D-21: no NATS
module is imported here). Loss is tolerated, blocking a read is not.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Literal

import structlog

from dc_core.events import (
    ACCESS_STREAM,
    EventPublisher,
    JetStreamPublisher,
    encode_envelope,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from dc_core.app import LifespanHook
    from dc_core.settings import CoreSettings

_log = structlog.get_logger("dc_core.access")

DEFAULT_CAPACITY = 10_000
DEFAULT_BATCH = 100
DEFAULT_FLUSH_S = 1.0
DEFAULT_PUBLISH_TIMEOUT_S = 10.0
DEFAULT_MAX_BACKOFF_S = 30.0
WARN_EVERY = 1000

# Why an event was dropped: the queue was full (the oldest goes), a publish failed or timed out
# (the rest of that batch goes, never re-queued), the envelope could not be encoded, or the app
# shut down before it was published.
DropReason = Literal["queue_full", "publish_failed", "invalid", "shutdown"]


class AccessLog:
    """Drop-oldest queue of ``(subject, envelope)`` pairs and the loop that publishes them.

    ``dropped`` counts every lost event and ``dropped_by_reason`` splits it by
    :data:`DropReason`. A structured warning ``access_events_dropped`` (with the counter) is logged
    on the first drop and every time the counter crosses another multiple of 1000.
    """

    def __init__(
        self,
        publisher: EventPublisher,
        *,
        capacity: int = DEFAULT_CAPACITY,
        batch: int = DEFAULT_BATCH,
        flush_s: float = DEFAULT_FLUSH_S,
        publish_timeout_s: float = DEFAULT_PUBLISH_TIMEOUT_S,
        max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
    ) -> None:
        if capacity < 1 or batch < 1 or flush_s <= 0 or publish_timeout_s <= 0:
            raise ValueError("capacity and batch must be >= 1, flush_s and timeouts > 0")
        self._publisher = publisher
        self._queue: deque[tuple[str, dict[str, Any]]] = deque(maxlen=capacity)
        self._batch = batch
        self._flush_s = flush_s
        self._publish_timeout_s = publish_timeout_s
        self._max_backoff_s = max(max_backoff_s, flush_s)
        self._wake = asyncio.Event()
        self.dropped = 0
        self.dropped_by_reason: dict[str, int] = {}

    @property
    def pending(self) -> int:
        """Events waiting to be published."""
        return len(self._queue)

    def _count_drop(self, count: int, reason: DropReason) -> None:
        if count <= 0:
            return
        before = self.dropped
        self.dropped += count
        self.dropped_by_reason[reason] = self.dropped_by_reason.get(reason, 0) + count
        if before == 0 or before // WARN_EVERY != self.dropped // WARN_EVERY:
            _log.warning("access_events_dropped", dropped=self.dropped, reason=reason)

    def record(self, subject: str, envelope: dict[str, Any]) -> None:
        """Enqueue one event; synchronous, O(1), never raises, never awaits."""
        try:
            if len(self._queue) == self._queue.maxlen:
                self._count_drop(1, "queue_full")  # the append below pushes the oldest out
            self._queue.append((subject, envelope))
            if len(self._queue) >= self._batch:
                self._wake.set()
        except Exception:  # noqa: S110 - a read must never fail because of its access event
            pass

    async def _publish_batch(self) -> Exception | None:
        """Publish up to ``batch`` queued events; the first failure drops the rest of the batch.

        Returns the failure (None on success). A cancellation (shutdown, flush deadline) counts
        what was taken but not published as ``shutdown`` and propagates.
        """
        items = [self._queue.popleft() for _ in range(min(self._batch, len(self._queue)))]
        for index, (subject, envelope) in enumerate(items):
            try:
                payload = encode_envelope(envelope)
                msg_id = str(envelope["event_id"])
            except Exception:
                self._count_drop(1, "invalid")
                continue
            try:
                await asyncio.wait_for(
                    self._publisher.publish(subject, payload, msg_id=msg_id),
                    self._publish_timeout_s,
                )
            except asyncio.CancelledError:
                self._count_drop(len(items) - index, "shutdown")
                raise
            except Exception as exc:
                self._count_drop(len(items) - index, "publish_failed")
                return exc
        return None

    async def run(self) -> None:
        """Flusher loop (cancellable): wait for a full batch or the flush interval, publish.

        After a failed batch the loop sleeps with a doubling backoff (``flush_s`` up to
        ``max_backoff_s``) and resets it after the next successful batch.
        """
        backoff = self._flush_s
        while True:
            try:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self._flush_s)
                self._wake.clear()
                while self._queue:
                    failure = await self._publish_batch()
                    if failure is None:
                        backoff = self._flush_s
                        continue
                    # Only the error class: never a payload.
                    _log.warning(
                        "access_events_publish_failed",
                        error=type(failure).__name__,
                        backoff_s=backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self._max_backoff_s)
                    break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the loop alive whatever happens
                _log.warning("access_log_loop_error", error=type(exc).__name__)
                await asyncio.sleep(backoff)

    async def flush(self, timeout_s: float) -> None:
        """Publish what is queued within ``timeout_s``; the last call on shutdown.

        Returns within the deadline even with a hanging publisher. Whatever is still queued
        afterwards (deadline reached or the publisher failing) is dropped as ``shutdown``.
        """
        try:
            async with asyncio.timeout(timeout_s):
                while self._queue:
                    if await self._publish_batch() is not None:
                        break
        except Exception:  # noqa: S110 - best effort: a deadline or a failing publisher
            pass
        finally:
            remaining = len(self._queue)
            self._queue.clear()
            self._count_drop(remaining, "shutdown")


def access_log_hook(settings: CoreSettings, *, flush_timeout_s: float = 2.0) -> LifespanHook:
    """Run an :class:`AccessLog` on the ``ACCESS`` stream when ``settings.nats_url`` is set.

    Sets ``app.state.access_log``; it is None without NATS, which makes every producer helper a
    no-op. On shutdown the flusher stops, the queue is flushed best-effort within
    ``flush_timeout_s`` and the publisher is closed.
    """

    @asynccontextmanager
    async def _hook(app: FastAPI) -> AsyncIterator[None]:
        if not settings.nats_url:
            app.state.access_log = None
            yield
            return
        publisher = JetStreamPublisher(
            settings.nats_url,
            user=settings.nats_user,
            password=settings.nats_password.get_secret_value() if settings.nats_password else None,
            stream=ACCESS_STREAM,
        )
        access_log = AccessLog(publisher)
        task = asyncio.create_task(access_log.run(), name="access-log-flusher")
        app.state.access_log = access_log
        try:
            yield
        finally:
            app.state.access_log = None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await access_log.flush(flush_timeout_s)
            await publisher.close()

    return _hook


__all__ = ["AccessLog", "access_log_hook"]
