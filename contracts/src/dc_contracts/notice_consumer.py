"""JetStream side of card notices (the only NATS-aware contracts module besides the relay, D-99).

Every contracts replica runs the durable pull consumer ``contracts-notices`` on stream ``AUDIT``,
filtered to ``audit.contracts.>`` (replicas share it as a work queue) and hands each message to the
transport-agnostic ``notices.handle_payload``. A message is acknowledged only after the
materialising transaction committed, so a crash between the two redelivers it and the idempotent
materialisation makes that harmless.

This module never creates, edits, purges or deletes the stream: the audit service owns ``AUDIT``.
While the stream is missing the loop backs off and retries. The contracts NATS user is allowed to
create exactly this consumer with exactly this filter and nothing else (``infra/nats``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import nats
import nats.errors
import structlog
from dc_core.app import LifespanHook
from dc_core.events import AUDIT_STREAM
from fastapi import FastAPI
from nats.js.api import AckPolicy, ConsumerConfig
from nats.js.errors import FetchTimeoutError

from dc_contracts.notices import InvalidNoticeEvent, handle_payload
from dc_contracts.settings import ContractsSettings

if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient
    from nats.aio.msg import Msg
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger("dc_contracts.notice_consumer")

NOTICE_DURABLE = "contracts-notices"
NOTICE_FILTER = "audit.contracts.>"


def _event_id(data: bytes) -> str | None:
    """The envelope's event id when the payload parses far enough to name one."""
    try:
        value = json.loads(data).get("event_id")
    except ValueError, AttributeError:
        return None
    return value if isinstance(value, str) else None


class NoticeConsumer:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: ContractsSettings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._nc: NatsClient | None = None
        self._sub: Any = None

    async def _subscription(self) -> Any:
        if self._sub is not None:
            return self._sub
        url = self._settings.nats_url
        if not url:
            raise RuntimeError("NoticeConsumer needs settings.nats_url")
        password = self._settings.nats_password
        nc = await nats.connect(
            servers=[url],
            user=self._settings.nats_user,
            password=password.get_secret_value() if password else None,
            max_reconnect_attempts=-1,
        )
        try:
            # Default deliver policy "all": a first start replays the stream. Events written
            # before notices existed carry no notify_card_ids, and dedupe makes replays harmless.
            self._sub = await nc.jetstream().pull_subscribe(
                NOTICE_FILTER,
                durable=NOTICE_DURABLE,
                stream=AUDIT_STREAM,
                config=ConsumerConfig(
                    durable_name=NOTICE_DURABLE,
                    ack_policy=AckPolicy.EXPLICIT,
                    max_deliver=-1,
                    ack_wait=self._settings.notice_consumer_ack_wait_s,
                ),
            )
        except BaseException:
            await nc.close()
            raise
        self._nc = nc
        return self._sub

    async def close(self) -> None:
        nc, self._nc, self._sub = self._nc, None, None
        if nc is not None and not nc.is_closed:
            try:
                await nc.close()
            except Exception as exc:
                _log.warning("notice_consumer_close_failed", error=type(exc).__name__)

    async def consume_once(self, timeout_s: float = 5.0) -> int:
        """Fetch and process one batch; returns the number of messages handled (0 on timeout)."""
        sub = await self._subscription()
        try:
            msgs = await sub.fetch(self._settings.notice_consumer_batch, timeout=timeout_s)
        except nats.errors.TimeoutError, FetchTimeoutError:
            return 0  # an empty fetch is normal
        for msg in msgs:
            await self._process(msg)
        return len(msgs)

    async def _process(self, msg: Msg) -> None:
        try:
            await handle_payload(
                self._session_factory, msg.data, subject=msg.subject, settings=self._settings
            )
        except Exception as exc:
            await self._on_failure(msg, exc)
        else:
            await msg.ack()

    async def _on_failure(self, msg: Msg, exc: Exception) -> None:
        """Never let one message stall the stream; log identifiers and the error class only."""
        if isinstance(exc, InvalidNoticeEvent):
            # Spoofed or malformed: no retry can fix it.
            _log.warning("notice_event_invalid", subject=msg.subject, error=type(exc).__name__)
            await msg.ack()
            return
        delivered = msg.metadata.num_delivered
        if delivered >= self._settings.notice_consumer_max_deliver:
            _log.error(
                "notice_event_dropped",
                event_id=_event_id(msg.data),
                subject=msg.subject,
                deliveries=delivered,
                error=type(exc).__name__,
            )
            await msg.ack()
            return
        _log.warning(
            "notice_event_retry",
            subject=msg.subject,
            deliveries=delivered,
            error=type(exc).__name__,
        )
        delay = min(float(2**delivered), self._settings.notice_consumer_max_backoff_s)
        await msg.nak(delay=delay)

    async def run(self) -> None:
        """Consume until cancelled. Connection and database errors back off and retry."""
        backoff = 0.5
        try:
            while True:
                try:
                    await self.consume_once(timeout_s=5.0)
                    backoff = 0.5
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _log.warning("notice_consumer_loop_error", error=type(exc).__name__)
                    await self.close()  # reconnect and re-subscribe on the next pass
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
        finally:
            await self.close()


def notice_consumer_hook(settings: ContractsSettings) -> LifespanHook:
    """Lifespan hook running the consumer when ``settings.nats_url`` is set."""

    @asynccontextmanager
    async def _hook(app: FastAPI) -> AsyncIterator[None]:
        if not settings.nats_url:
            yield
            return
        consumer = NoticeConsumer(app.state.session_factory, settings)
        task = asyncio.create_task(consumer.run(), name="contracts-notice-consumer")
        app.state.notice_consumer = consumer
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return _hook
