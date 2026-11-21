"""Fixtures: audit database + a probe producer outbox + NATS, all on real containers."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import nats
import pytest
import uuid_utils.compat as uuid_compat
from dc_audit.consumer import AuditConsumer
from dc_audit.migrate import SCRIPT_LOCATION, VERSION_TABLE
from dc_audit.settings import AuditSettings
from dc_core.actor import Actor
from dc_core.db import make_async_engine, make_session_factory, new_metadata
from dc_core.events import JetStreamPublisher
from dc_core.migrations import run_alembic
from dc_core.relay import OutboxRelay
from dc_core.uow import OutboxMixin, UnitOfWork
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

ADMIN = Actor(type="user", id="fake:admin", display="Admin Display", via="jwt", credential_id="j1")


class ProbeBase(DeclarativeBase):
    metadata = new_metadata()


class ProbeOutbox(ProbeBase, OutboxMixin):
    """A producer's outbox (the probe service), in the same database as the audit tables."""

    __tablename__ = "outbox"


@dataclass
class AuditEnv:
    db_url: str
    nats_url: str
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    settings: AuditSettings

    @property
    def outbox_model(self) -> type[ProbeOutbox]:
        return ProbeOutbox

    @property
    def actor(self) -> Actor:
        return ADMIN

    def publisher(self, **kwargs: Any) -> JetStreamPublisher:
        return JetStreamPublisher(self.nats_url, **kwargs)

    def relay(self, publisher: Any, **kwargs: Any) -> OutboxRelay:
        return OutboxRelay(
            self.session_factory, ProbeOutbox, service="probe", publisher=publisher, **kwargs
        )

    def consumer(self, **kwargs: Any) -> AuditConsumer:
        return AuditConsumer(self.session_factory, self.settings, **kwargs)

    async def write_event(
        self,
        action: str = "widget.created",
        *,
        aggregate_id: str | None = None,
        after: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        commit: bool = True,
        uow: UnitOfWork | None = None,
    ) -> tuple[uuid.UUID, str]:
        """One audited write in a producer transaction; returns (event_id, aggregate_id)."""
        agg = aggregate_id or str(uuid_compat.uuid7())
        own = uow or UnitOfWork(
            self.session_factory, actor=ADMIN, outbox_model=ProbeOutbox, service="probe"
        )
        await own.__aenter__()
        try:
            own.audit(action, "widget", agg, before=before, after=after or {"id": agg, "name": "w"})
            await own.commit()
            event_id = await self._last_event_id(agg)
        finally:
            await own.__aexit__(None, None, None)
        return event_id, agg

    async def _last_event_id(self, aggregate_id: str) -> uuid.UUID:
        rows = await self.fetch(
            "SELECT event_id FROM outbox WHERE aggregate_id = :a ORDER BY id DESC LIMIT 1",
            a=aggregate_id,
        )
        value: uuid.UUID = rows[0]["event_id"]
        return value

    async def fetch(self, query: str, **params: Any) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            result = await conn.execute(text(query), params)
            return [dict(row._mapping) for row in result]


@pytest.fixture
async def audit_env(fresh_db_url: str, nats_url: str) -> AsyncIterator[AuditEnv]:
    run_alembic(
        script_location=SCRIPT_LOCATION,
        version_table=VERSION_TABLE,
        url=fresh_db_url,
        command="upgrade",
    )
    sync_engine = create_engine(fresh_db_url)
    ProbeBase.metadata.create_all(sync_engine)
    sync_engine.dispose()
    engine = make_async_engine(fresh_db_url)
    env = AuditEnv(
        db_url=fresh_db_url,
        nats_url=nats_url,
        engine=engine,
        session_factory=make_session_factory(engine),
        settings=AuditSettings(database_url=fresh_db_url, nats_url=nats_url),  # type: ignore[arg-type]
    )
    yield env
    await engine.dispose()


@pytest.fixture
def eventually() -> Callable[..., Awaitable[Any]]:
    """``await eventually(coro_fn, within_s=10)``: retry until it returns something truthy."""

    async def _eventually(
        probe: Callable[[], Awaitable[Any]], *, within_s: float = 10.0, interval: float = 0.1
    ) -> Any:
        deadline = time.monotonic() + within_s
        while True:
            value = await probe()
            if value:
                return value
            if time.monotonic() >= deadline:
                raise TimeoutError(f"condition not met within {within_s} s")
            await asyncio.sleep(interval)

    return _eventually


@pytest.fixture
async def run_consumer() -> AsyncIterator[Callable[[AuditConsumer], asyncio.Task[None]]]:
    """Start ``consumer.run()`` in the background; cancelled and awaited on teardown."""
    tasks: list[asyncio.Task[None]] = []

    def _start(consumer: AuditConsumer) -> asyncio.Task[None]:
        task = asyncio.create_task(consumer.run())
        tasks.append(task)
        return task

    yield _start
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
async def stream_exists(nats_url: str) -> Callable[[], Awaitable[bool]]:
    async def _exists() -> bool:
        nc = await nats.connect(servers=[nats_url])
        try:
            with contextlib.suppress(Exception):
                await nc.jetstream().stream_info("AUDIT")
                return True
            return False
        finally:
            await nc.close()

    return _exists
