"""Outbox relay on real Postgres 18 and the JetStream transport on a real NATS 2.15 server."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import nats
import pytest
import structlog
import uuid_utils.compat as uuid_compat
from dc_core.access import access_log_hook
from dc_core.actor import Actor
from dc_core.app import create_app, read_session
from dc_core.db import make_async_engine, make_session_factory, new_metadata
from dc_core.events import (
    ACCESS_STREAM,
    AUDIT_STREAM,
    JetStreamPublisher,
    PublishAck,
    build_access_envelope,
    subject_for,
)
from dc_core.relay import OutboxRelay
from dc_core.settings import CoreSettings
from dc_core.testing import NatsOutage
from dc_core.uow import AuditColumnsMixin, OutboxMixin, UnitOfWork
from fastapi import FastAPI
from nats.js.api import StreamConfig
from sqlalchemy import Text, select, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

pytestmark = pytest.mark.integration

ALICE = Actor(type="user", id="fake:alice", display="Alice", via="jwt")
SUBJECT = "audit.probe.widget.widget.created"


class Base(DeclarativeBase):
    metadata = new_metadata()


class Widget(Base, AuditColumnsMixin):
    __tablename__ = "widgets"
    __audit_fields__ = ("id", "name")

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class RelayOutbox(Base, OutboxMixin):
    __tablename__ = "outbox"


@pytest.fixture
async def factory(fresh_db_url: str) -> AsyncIterator[Any]:
    engine = make_async_engine(fresh_db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


class Recording:
    """Publisher that records (subject, msg_id, payload) and fails while ``failures`` > 0."""

    def __init__(self, *, fail_at: int | None = None, failures: int = 0) -> None:
        self.published: list[tuple[str, str, dict[str, Any]]] = []
        self.calls = 0
        self.fail_at = fail_at
        self.failures = failures

    async def publish(self, subject: str, payload: bytes, *, msg_id: str) -> PublishAck:
        self.calls += 1
        if self.calls == self.fail_at or self.failures > 0:
            self.failures = max(self.failures - 1, 0)
            raise ConnectionError("broker down")
        self.published.append((subject, msg_id, json.loads(payload)))
        return PublishAck(duplicate=False)

    async def close(self) -> None:
        return None


async def write_widgets(factory: Any, count: int) -> list[str]:
    """``count`` audited creates, one unit of work each; returns their outbox event ids."""
    for n in range(count):
        wid = uuid_compat.uuid7()
        async with UnitOfWork(
            factory, actor=ALICE, outbox_model=RelayOutbox, service="probe", request_id=f"r{n}"
        ) as uow:
            uow.session.add(Widget(id=wid, name=f"w{n}"))
            uow.audit("widget.created", "widget", wid, before=None, after={"name": f"w{n}"})
            await uow.commit()
    async with factory() as session:
        rows = (await session.execute(select(RelayOutbox).order_by(RelayOutbox.id))).scalars()
        return [str(row.event_id) for row in rows]


async def outbox_rows(factory: Any) -> list[RelayOutbox]:
    async with factory() as session:
        return list((await session.execute(select(RelayOutbox).order_by(RelayOutbox.id))).scalars())


async def widget_rows(factory: Any) -> list[Widget]:
    async with factory() as session:
        return list((await session.execute(select(Widget))).scalars())


async def eventually(check: Callable[[], Awaitable[bool]], within_s: float = 10.0) -> None:
    deadline = time.monotonic() + within_s
    while not await check():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.05)


def relay(factory: Any, publisher: Any, **kwargs: Any) -> OutboxRelay:
    return OutboxRelay(factory, RelayOutbox, service="probe", publisher=publisher, **kwargs)


@asynccontextmanager
async def running(target: OutboxRelay) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.create_task(target.run())
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --- drain_once -------------------------------------------------------------------------------


async def test_drain_publishes_in_id_order_with_the_event_id_and_marks_rows(factory: Any) -> None:
    event_ids = await write_widgets(factory, 3)
    publisher = Recording()
    assert await relay(factory, publisher).drain_once() == 3
    assert [(s, m) for s, m, _ in publisher.published] == [(SUBJECT, e) for e in event_ids]
    envelope = publisher.published[0][2]
    assert envelope["event_id"] == event_ids[0]
    assert envelope["service"] == "probe"
    assert envelope["actor"]["id"] == "fake:alice"
    assert envelope["after"] == {"name": "w0"}
    assert all(row.published_at is not None for row in await outbox_rows(factory))
    assert await relay(factory, publisher).drain_once() == 0
    assert len(publisher.published) == 3


async def test_a_failed_publish_commits_earlier_rows_counts_the_attempt_and_raises(
    factory: Any,
) -> None:
    event_ids = await write_widgets(factory, 3)
    with structlog.testing.capture_logs() as captured:
        with pytest.raises(ConnectionError):
            await relay(factory, Recording(fail_at=2)).drain_once()
    rows = await outbox_rows(factory)
    assert [row.published_at is not None for row in rows] == [True, False, False]
    assert [row.attempts for row in rows] == [0, 1, 0]
    failed = [e for e in captured if e["event"] == "outbox_relay_publish_failed"]
    assert [(e["event_id"], e["attempts"], e["error"]) for e in failed] == [
        (event_ids[1], 1, "ConnectionError")
    ]
    healthy = Recording()
    assert await relay(factory, healthy).drain_once() == 2
    assert [m for _, m, _ in healthy.published] == event_ids[1:]


# --- the advisory lock and the loop ------------------------------------------------------------


async def test_only_the_replica_holding_the_lock_relays(factory: Any) -> None:
    leader = relay(factory, Recording())
    lock = await leader._try_lock()
    assert lock is not None
    follower_publisher = Recording()
    follower = relay(factory, follower_publisher, poll_s=0.05)
    assert await follower._try_lock() is None
    async with running(follower):
        await write_widgets(factory, 1)
        await asyncio.sleep(0.3)
        assert follower_publisher.published == []  # waits while another replica holds the lock
        await OutboxRelay._close_lock(lock)

        async def _relayed() -> bool:
            return len(follower_publisher.published) == 1

        await eventually(_relayed)


async def test_the_loop_reacquires_a_lost_lock_and_releases_it_when_cancelled(
    factory: Any,
) -> None:
    publisher = Recording()
    # batch=1: a full batch drains again at once, without the poll sleep.
    worker = relay(factory, publisher, poll_s=0.05, batch=1)
    async with running(worker):
        await write_widgets(factory, 2)

        async def _two() -> bool:
            return len(publisher.published) == 2

        await eventually(_two)
        # Kill the lock connection on the server side: the loop must notice and lock again.
        async with factory() as session:
            killed = (
                await session.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_locks "
                        "WHERE locktype = 'advisory' AND granted"
                    )
                )
            ).scalars()
            assert list(killed) == [True]
        await write_widgets(factory, 1)

        async def _three() -> bool:
            return len(publisher.published) == 3

        await eventually(_three)
    # Cancelling the loop closed its lock connection, so another replica can take over.
    lock = await relay(factory, Recording())._try_lock()
    assert lock is not None
    await OutboxRelay._close_lock(lock)


async def test_the_loop_backs_off_while_publishing_fails_and_then_recovers(factory: Any) -> None:
    await write_widgets(factory, 1)
    publisher = Recording(failures=4)
    worker = relay(factory, publisher, poll_s=0.02, max_backoff_s=0.08)
    with structlog.testing.capture_logs() as captured:
        async with running(worker):

            async def _published() -> bool:
                # Wait for the row to be marked too: the publish lands before the mark commits,
                # and stopping the loop in between would leave published_at empty.
                rows = await outbox_rows(factory)
                return len(publisher.published) == 1 and rows[0].published_at is not None

            await eventually(_published)
    backoffs = [e["backoff_s"] for e in captured if e["event"] == "outbox_relay_loop_error"]
    assert backoffs == [0.02, 0.04, 0.08, 0.08]
    (row,) = await outbox_rows(factory)
    assert row.attempts == 4
    assert row.published_at is not None


# --- read-only sessions -------------------------------------------------------------------------


async def test_read_session_reads_and_refuses_any_write(factory: Any) -> None:
    await write_widgets(factory, 1)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=factory)))
    seen: list[list[str]] = []
    async for session in read_session(request):  # type: ignore[arg-type]
        seen.append([w.name for w in (await session.execute(select(Widget))).scalars()])
        session.add(Widget(id=uuid_compat.uuid7(), name="sneaky"))
        with pytest.raises(RuntimeError, match="read-only session"):
            await session.flush()
        await session.rollback()
    assert seen == [["w0"]]  # exactly one session, closed when the dependency finished
    assert [w.name for w in await widget_rows(factory)] == ["w0"]


# --- JetStream ----------------------------------------------------------------------------------


async def add_streams(url: str) -> None:
    nc = await nats.connect(servers=[url])
    try:
        js = nc.jetstream()
        await js.add_stream(StreamConfig(name=AUDIT_STREAM, subjects=["audit.>"]))
        await js.add_stream(StreamConfig(name=ACCESS_STREAM, subjects=["access.>"]))
    finally:
        await nc.close()


async def stream_messages(url: str, stream: str) -> list[Any]:
    nc = await nats.connect(servers=[url])
    try:
        js = nc.jetstream()
        count = (await js.stream_info(stream)).state.messages
        return [await js.get_msg(stream, seq) for seq in range(1, count + 1)]
    finally:
        await nc.close()


async def test_jetstream_publisher_sets_the_msg_id_and_reports_duplicates(nats_url: str) -> None:
    await add_streams(nats_url)
    publisher = JetStreamPublisher(nats_url)
    try:
        first = await publisher.publish(SUBJECT, b'{"n":1}', msg_id="evt-1")
        again = await publisher.publish(SUBJECT, b'{"n":1}', msg_id="evt-1")
    finally:
        await publisher.close()
    assert (first.duplicate, again.duplicate) == (False, True)
    (message,) = await stream_messages(nats_url, AUDIT_STREAM)
    assert message.subject == SUBJECT
    assert message.data == b'{"n":1}'
    assert message.headers["Nats-Msg-Id"] == "evt-1"


async def test_create_app_relays_the_outbox_to_jetstream_and_is_ready(
    nats_url: str, factory: Any, asgi_client: Any
) -> None:
    await add_streams(nats_url)
    settings = CoreSettings(service="probe", nats_url=nats_url)
    app = create_app(
        service="probe",
        settings=settings,
        routers=[],
        session_factory=factory,
        outbox_model=RelayOutbox,
    )
    async with asgi_client(app) as client:
        assert isinstance(app.state.outbox_relay, OutboxRelay)
        ready = await client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ok"}
        event_ids = await write_widgets(factory, 2)

        async def _arrived() -> bool:
            return len(await stream_messages(nats_url, AUDIT_STREAM)) == 2

        await eventually(_arrived, within_s=15)
    messages = await stream_messages(nats_url, AUDIT_STREAM)
    assert [m.headers["Nats-Msg-Id"] for m in messages] == event_ids
    assert {m.subject for m in messages} == {SUBJECT}
    assert json.loads(messages[0].data)["event_id"] == event_ids[0]


async def test_the_access_log_hook_flushes_recorded_events_to_the_access_stream(
    nats_url: str,
) -> None:
    await add_streams(nats_url)
    envelope = build_access_envelope(
        service="probe",
        aggregate_type="widget",
        aggregate_id="w-1",
        action="widget.read",
        actor={"type": "user", "id": "fake:alice"},
        scope_path=(),
        request_id="req-a",
        detail={"format": "json"},
    )
    app = FastAPI()
    async with access_log_hook(CoreSettings(service="probe", nats_url=nats_url))(app):
        app.state.access_log.record(
            subject_for("probe", "widget", "widget.read", prefix="access"), envelope
        )
    (message,) = await stream_messages(nats_url, ACCESS_STREAM)
    assert message.subject == "access.probe.widget.widget.read"
    assert message.headers["Nats-Msg-Id"] == envelope["event_id"]
    assert json.loads(message.data) == envelope


async def test_a_publish_during_a_broker_outage_times_out_and_works_again_afterwards(
    nats_outage: NatsOutage,
) -> None:
    await add_streams(nats_outage.url)
    publisher = JetStreamPublisher(nats_outage.url, timeout_s=1.0)
    try:
        assert (await publisher.publish(SUBJECT, b"{}", msg_id="before")).duplicate is False
        nats_outage.pause()
        with pytest.raises(asyncio.TimeoutError):
            await publisher.publish(SUBJECT, b"{}", msg_id="during")
        nats_outage.unpause()
        assert (await publisher.publish(SUBJECT, b"{}", msg_id="after")).duplicate is False
    finally:
        await publisher.close()
    ids = [m.headers["Nats-Msg-Id"] for m in await stream_messages(nats_outage.url, AUDIT_STREAM)]
    assert ids[0] == "before"
    assert ids[-1] == "after"
