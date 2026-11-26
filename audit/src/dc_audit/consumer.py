"""JetStream side of the audit service (the only NATS-aware audit module, D-21).

Creates stream ``AUDIT`` (subjects ``audit.>``) and stream ``ACCESS`` (subjects ``access.>``,
bounded by max age and max bytes with discard old, so best-effort reads can never fill the
JetStream store that the guaranteed AUDIT stream shares; WR-03) if they do not exist, brings an
existing stream's requested limits up to date, runs the durable pull consumers
``audit-writer`` and ``audit-access-writer`` and hands every message to the transport-agnostic
``handle_payload``.
A message that keeps failing is written to ``audit_dead_letter`` and acknowledged once it has been
delivered ``max_deliver`` times, so it can never block the stream (T-01-20).
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import nats
import nats.errors
import structlog
from dc_core.app import LifespanHook
from dc_core.events import ACCESS_STREAM, AUDIT_STREAM
from fastapi import FastAPI
from nats.js.api import AckPolicy, ConsumerConfig, DiscardPolicy, StorageType, StreamConfig
from nats.js.errors import FetchTimeoutError, NotFoundError
from sqlalchemy.dialects.postgresql import insert

from dc_audit.ingest import handle_payload
from dc_audit.models import INGEST_ACTOR, AuditDeadLetter
from dc_audit.settings import AuditSettings

if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient
    from nats.aio.msg import Msg
    from nats.js import JetStreamContext
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger("dc_audit.consumer")
_AUDIT_SUBJECTS = ("audit.>",)
_ACCESS_SUBJECTS = ("access.>",)
_ACK_WAIT_S = 30
_ERROR_MAX_CHARS = 2000


def _limits(max_age_s: float | None, max_bytes: int | None, discard_old: bool) -> dict[str, Any]:
    """The stream limits a caller asked for, as ``StreamConfig`` fields (none for AUDIT)."""
    limits: dict[str, Any] = {}
    if max_age_s is not None:
        limits["max_age"] = max_age_s
    if max_bytes is not None:
        limits["max_bytes"] = max_bytes
    if discard_old:
        limits["discard"] = DiscardPolicy.OLD
    return limits


async def ensure_stream(
    js: JetStreamContext,
    name: str = AUDIT_STREAM,
    subjects: tuple[str, ...] = _AUDIT_SUBJECTS,
    *,
    max_age_s: float | None = None,
    max_bytes: int | None = None,
    discard_old: bool = False,
) -> None:
    """Create the stream when missing; on an existing one, update only the requested limits.

    Nothing else of an existing stream is ever edited (subjects, storage, duplicate window), and a
    stream created with no limits requested (AUDIT) is left exactly as it is. A limit requested
    here that differs from the existing stream's (for example ACCESS created before WR-03 added
    ``max_bytes``) is applied with ``update_stream``, so no recreate is needed.
    """
    limits = _limits(max_age_s, max_bytes, discard_old)
    try:
        info = await js.stream_info(name)
    except NotFoundError:
        config = StreamConfig(
            name=name,
            subjects=list(subjects),
            storage=StorageType.FILE,
            duplicate_window=120,
            **limits,
        )
        await js.add_stream(config)
        return
    current = info.config
    if any(getattr(current, field) != value for field, value in limits.items()):
        for field, value in limits.items():
            setattr(current, field, value)
        await js.update_stream(current)
        _log.info(
            "audit_stream_limits_updated", stream=name, **{k: str(v) for k, v in limits.items()}
        )


def _payload_text(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return "base64:" + base64.b64encode(payload).decode("ascii")


class AuditConsumer:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: AuditSettings,
        *,
        max_nak_delay_s: float = 30.0,
        stream: str = AUDIT_STREAM,
        subjects: tuple[str, ...] = _AUDIT_SUBJECTS,
        durable: str | None = None,
        batch: int | None = None,
        max_age_s: float | None = None,
        max_bytes: int | None = None,
        discard_old: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._max_nak_delay_s = max_nak_delay_s
        self._stream = stream
        self._subjects = subjects
        self._durable = durable or settings.consumer_durable
        self._batch = batch or settings.consumer_batch
        self._max_age_s = max_age_s
        self._max_bytes = max_bytes
        self._discard_old = discard_old
        self._nc: NatsClient | None = None
        self._sub: Any = None

    async def _subscription(self) -> Any:
        if self._sub is not None:
            return self._sub
        url = self._settings.nats_url
        if not url:
            raise RuntimeError("AuditConsumer needs settings.nats_url")
        password = self._settings.nats_password
        nc = await nats.connect(
            servers=[url],
            user=self._settings.nats_user,
            password=password.get_secret_value() if password else None,
            max_reconnect_attempts=-1,
        )
        try:
            js = nc.jetstream()
            await ensure_stream(
                js,
                self._stream,
                self._subjects,
                max_age_s=self._max_age_s,
                max_bytes=self._max_bytes,
                discard_old=self._discard_old,
            )
            self._sub = await js.pull_subscribe(
                self._subjects[0],
                durable=self._durable,
                stream=self._stream,
                config=ConsumerConfig(
                    durable_name=self._durable,
                    ack_policy=AckPolicy.EXPLICIT,
                    max_deliver=self._settings.max_deliver,
                    ack_wait=_ACK_WAIT_S,
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
                _log.warning("audit_consumer_close_failed", error=type(exc).__name__)

    async def consume_once(self, timeout_s: float = 5.0) -> int:
        """Fetch and process one batch; returns the number of messages handled (0 on timeout)."""
        sub = await self._subscription()
        try:
            msgs = await sub.fetch(self._batch, timeout=timeout_s)
        except nats.errors.TimeoutError, FetchTimeoutError:
            return 0  # an empty fetch is normal
        for msg in msgs:
            await self._process(msg)
        return len(msgs)

    async def _process(self, msg: Msg) -> None:
        try:
            await handle_payload(self._session_factory, msg.data, subject=msg.subject)
        except Exception as exc:
            await self._on_failure(msg, exc)
        else:
            await msg.ack()

    async def _on_failure(self, msg: Msg, exc: Exception) -> None:
        delivered = msg.metadata.num_delivered
        _log.warning(
            "audit_ingest_failed",
            subject=msg.subject,
            deliveries=delivered,
            error=type(exc).__name__,
        )
        if delivered >= self._settings.max_deliver:
            try:
                await self._dead_letter(msg, exc, delivered)
            except Exception as dl_exc:
                _log.error("audit_dead_letter_failed", error=type(dl_exc).__name__)
            else:
                await msg.ack()
                return
        await msg.nak(delay=min(float(2**delivered), self._max_nak_delay_s))

    async def _dead_letter(self, msg: Msg, exc: Exception, delivered: int) -> None:
        async with self._session_factory() as session:
            await session.execute(
                insert(AuditDeadLetter).values(
                    subject=msg.subject,
                    payload=_payload_text(msg.data),
                    error=f"{type(exc).__name__}: {exc}"[:_ERROR_MAX_CHARS],
                    deliveries=delivered,
                    created_by=INGEST_ACTOR,
                    updated_by=INGEST_ACTOR,
                )
            )
            await session.commit()

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
                    _log.warning("audit_consumer_loop_error", error=type(exc).__name__)
                    await self.close()  # reconnect and re-subscribe on the next pass
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
        finally:
            await self.close()


def access_consumer(
    session_factory: async_sessionmaker[AsyncSession],
    settings: AuditSettings,
    *,
    max_nak_delay_s: float = 30.0,
) -> AuditConsumer:
    """The ``audit-access-writer`` consumer on stream ACCESS (``access.>``, bounded, D-171).

    Bounded by age and by size, discarding the oldest (WR-03): a wedged consumer loses old access
    events instead of filling the JetStream store that the guaranteed AUDIT stream shares.
    """
    return AuditConsumer(
        session_factory,
        settings,
        max_nak_delay_s=max_nak_delay_s,
        stream=ACCESS_STREAM,
        subjects=_ACCESS_SUBJECTS,
        durable=settings.access_consumer_durable,
        batch=settings.access_consumer_batch,
        max_age_s=float(settings.access_max_age_s),
        max_bytes=settings.access_max_bytes,
        discard_old=True,
    )


def consumer_hook(settings: AuditSettings) -> LifespanHook:
    """Lifespan hook running both consumers (AUDIT and ACCESS) when ``settings.nats_url`` is set."""

    @asynccontextmanager
    async def _hook(app: FastAPI) -> AsyncIterator[None]:
        if not settings.nats_url:
            yield
            return
        consumer = AuditConsumer(app.state.session_factory, settings)
        access = access_consumer(app.state.session_factory, settings)
        tasks = [
            asyncio.create_task(consumer.run(), name="audit-consumer"),
            asyncio.create_task(access.run(), name="audit-access-consumer"),
        ]
        app.state.audit_consumer = consumer
        app.state.audit_access_consumer = access
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return _hook
