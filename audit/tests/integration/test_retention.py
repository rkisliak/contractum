"""Audit retention (quick 261120-0ye, D-42): event-carried setting, floor, protected rows, lock,
batching and the summary row."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from dc_audit.migrate import SCRIPT_LOCATION, VERSION_TABLE
from dc_audit.retention import RetentionJob
from dc_audit.settings import AuditSettings
from dc_core.db import make_async_engine, make_session_factory
from dc_core.migrations import run_alembic
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


@dataclass
class Env:
    engine: AsyncEngine
    db_url: str
    job: Callable[..., RetentionJob]

    async def add(
        self,
        age_days: float,
        *,
        service: str = "probe",
        aggregate_type: str = "widget",
        action: str = "widget.updated",
        after: dict[str, Any] | None = None,
    ) -> None:
        when = datetime.now(UTC) - timedelta(days=age_days)
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO audit_event (event_id, service, aggregate_type, aggregate_id,"
                    " action, scope_path, actor, occurred_at, after, changes, schema_version,"
                    " category, created_by, updated_by) VALUES (:e, :s, :t, 'x', :a, '{}',"
                    " CAST(:actor AS jsonb), :w, CAST(:after AS jsonb), '[]', 1, 'change',"
                    " 'test', 'test')"
                ),
                {
                    "e": uuid.uuid4(),
                    "s": service,
                    "t": aggregate_type,
                    "a": action,
                    "actor": json.dumps({"type": "user", "id": "fake:a"}),
                    "w": when,
                    "after": json.dumps(after) if after is not None else None,
                },
            )

    async def setting(
        self, days: int | None, *, age_days: float = 0.0, action: str = "settings.updated"
    ) -> None:
        platform: dict[str, Any] = {} if days is None else {"audit_retention_days": days}
        await self.add(
            age_days,
            service="identity",
            aggregate_type="platform_settings",
            action=action,
            after={"platform": platform},
        )

    async def count(self, where: str = "true") -> int:
        async with self.engine.connect() as conn:
            row = await conn.execute(text(f"SELECT count(*) FROM audit_event WHERE {where}"))  # noqa: S608
            return int(row.scalar_one())


@pytest.fixture
async def env(fresh_db_url: str) -> AsyncIterator[Env]:
    run_alembic(
        script_location=SCRIPT_LOCATION,
        version_table=VERSION_TABLE,
        url=fresh_db_url,
        command="upgrade",
    )
    engine = make_async_engine(fresh_db_url)
    factory = make_session_factory(engine)

    def job(**overrides: Any) -> RetentionJob:
        settings = AuditSettings(database_url=fresh_db_url, **overrides)  # type: ignore[arg-type]
        return RetentionJob(factory, settings)

    yield Env(engine, fresh_db_url, job)
    await engine.dispose()


async def test_with_no_settings_event_and_no_env_value_nothing_is_deleted(env: Env) -> None:
    await env.add(2000)
    await env.add(10)
    result = await env.job().run_once()
    assert result is not None and result.days is None and result.deleted == 0
    assert await env.count() == 2


async def test_a_settings_event_deletes_older_rows_and_keeps_settings_and_writes_one_summary(
    env: Env,
) -> None:
    await env.add(200)
    await env.add(150)
    await env.add(99)
    await env.add(50)
    await env.setting(100, age_days=0)
    await env.setting(5, age_days=300, action="settings.imported")  # the history survives, too
    result = await env.job().run_once()
    assert result is not None and result.days == 100 and result.deleted == 2
    # the 200 and 150 day rows go; the 99 and 50 day rows are inside the 100 days
    assert await env.count("aggregate_type = 'widget'") == 2
    assert await env.count("aggregate_type = 'platform_settings'") == 2
    assert await env.count("aggregate_type = 'audit_retention'") == 1
    async with env.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT service, action, actor, scope_path, category, after, created_by"
                    " FROM audit_event WHERE aggregate_type = 'audit_retention'"
                )
            )
        ).one()
    assert (row.service, row.action, row.category) == ("audit", "audit.retention_applied", "change")
    assert row.actor["id"] == "system:audit-retention"
    assert row.scope_path == []
    assert row.after["retention_days"] == 100 and row.after["deleted"] == 2
    assert row.created_by == "system:audit-retention"

    # a second run finds nothing to delete, writes no second summary and never touches the first
    again = await env.job().run_once()
    assert again is not None and again.deleted == 0
    assert await env.count("aggregate_type = 'audit_retention'") == 1


async def test_a_stored_zero_keeps_everything_for_ever(env: Env) -> None:
    await env.add(5000)
    await env.setting(0)
    result = await env.job(retention_days=100).run_once()  # the env value would delete it
    assert result is not None and result.days is None and result.deleted == 0
    assert await env.count("aggregate_type = 'widget'") == 1


async def test_the_newest_settings_event_wins_and_absence_means_the_env_value(env: Env) -> None:
    await env.add(400)
    await env.setting(100, age_days=2)
    await env.setting(0, age_days=1)
    first = await env.job().run_once()
    assert first is not None and first.days is None and first.deleted == 0
    await env.setting(None, age_days=0)  # a platform save that resets the field: env applies
    second = await env.job(retention_days=200).run_once()
    assert second is not None and second.days == 200 and second.deleted == 1


async def test_the_floor_wins_over_a_lower_env_value_and_a_lower_stored_value(env: Env) -> None:
    await env.add(60)
    await env.add(100)
    result = await env.job(retention_days=30, retention_min_days=90).run_once()
    assert result is not None and result.days == 90 and result.deleted == 1
    assert await env.count("aggregate_type = 'widget'") == 1  # the 60-day row stays
    await env.setting(31)  # not reachable through the API (the model refuses it), but forged
    forged = await env.job(retention_min_days=90).run_once()
    assert forged is not None and forged.days == 90 and forged.deleted == 0


async def test_only_identity_settings_events_count(env: Env) -> None:
    await env.add(400)
    await env.add(
        1,
        service="probe",
        aggregate_type="platform_settings",
        action="settings.updated",
        after={"platform": {"audit_retention_days": 100}},
    )
    result = await env.job().run_once()
    assert result is not None and result.days is None and result.deleted == 0


async def test_a_second_concurrent_run_skips(env: Env) -> None:
    await env.add(400)
    await env.setting(100)
    holder = env.job()
    conn = await holder._lock()
    assert conn is not None
    try:
        assert await env.job().run_once() is None
        assert await env.count("aggregate_type = 'widget'") == 1
    finally:
        await holder._unlock(conn)
    result = await env.job().run_once()
    assert result is not None and result.deleted == 1


async def test_batching_respects_the_batch_cap_per_run(env: Env) -> None:
    for _ in range(10):
        await env.add(400)
    await env.setting(100)
    job = env.job(retention_batch=2, retention_max_batches=2)
    first = await job.run_once()
    assert first is not None and (first.deleted, first.batches) == (4, 2)
    assert await env.count("aggregate_type = 'widget'") == 6
    assert await env.count("aggregate_type = 'audit_retention'") == 1
    await job.run_once()
    assert await env.count("aggregate_type = 'widget'") == 2
    await job.run_once()
    assert await env.count("aggregate_type = 'widget'") == 0
    await job.run_once()  # nothing left: no further summary
    # one summary per run that deleted rows (three of the four runs)
    assert await env.count("aggregate_type = 'audit_retention'") == 3


async def test_the_delete_can_use_an_index_with_occurred_at_leading(env: Env) -> None:
    for _ in range(50):
        await env.add(400)
    async with env.engine.connect() as conn:
        await conn.execute(text("ANALYZE audit_event"))
        await conn.execute(text("SET enable_seqscan = off"))
        await conn.execute(text("SET enable_bitmapscan = off"))
        plan = "\n".join(
            row[0]
            for row in await conn.execute(
                text(
                    "EXPLAIN SELECT id FROM audit_event WHERE occurred_at < now()"
                    " AND aggregate_type <> ALL (ARRAY['platform_settings'])"
                    " ORDER BY occurred_at LIMIT 5000"
                )
            )
        )
    assert "ix_audit_event_time" in plan, plan
