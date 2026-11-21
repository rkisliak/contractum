"""``read_migration_head`` and the ``/internal/v1/about`` route on real Postgres 18."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from dc_auth.about import about_router, read_migration_head
from dc_auth.testing import FakeIssuer
from dc_core.db import make_async_engine, make_session_factory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

VERSION_TABLE = "probe_alembic_version"  # literal in the setup SQL below
HEAD = "0007_settings"


@pytest.fixture
async def engine(fresh_db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_async_engine(fresh_db_url)
    yield engine
    await engine.dispose()


async def _version_table(engine: AsyncEngine, *heads: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE probe_alembic_version (version_num varchar(32))"))
        for head in heads:
            await conn.execute(
                text("INSERT INTO probe_alembic_version (version_num) VALUES (:head)"),
                {"head": head},
            )


async def test_the_head_is_read_from_the_version_table(engine: AsyncEngine) -> None:
    await _version_table(engine, HEAD)
    assert await read_migration_head(make_session_factory(engine), VERSION_TABLE) == ("ok", HEAD)


async def test_an_empty_version_table_is_ok_without_a_head(engine: AsyncEngine) -> None:
    await _version_table(engine)
    assert await read_migration_head(make_session_factory(engine), VERSION_TABLE) == ("ok", None)


async def test_a_missing_version_table_is_unavailable_never_an_error(engine: AsyncEngine) -> None:
    state = await read_migration_head(make_session_factory(engine), VERSION_TABLE)
    assert state == ("unavailable", None)


async def test_the_route_reports_the_database_and_its_head(
    fresh_db_url: str,
    engine: AsyncEngine,
    fake_issuer: FakeIssuer,
    asgi_client: Any,
    probe_app: Any,
) -> None:
    await _version_table(engine, HEAD)
    app_engine = make_async_engine(fresh_db_url)
    factory = make_session_factory(app_engine)
    router = about_router("probe", session_factory=factory, version_table=VERSION_TABLE)
    app = probe_app(internal_routers=[router], database_url=fresh_db_url, session_factory=factory)
    headers = {"Authorization": f"Bearer {fake_issuer.mint('admin')}"}
    try:
        async with asgi_client(app) as client:
            response = await client.get("/internal/v1/about", headers=headers)
    finally:
        await app_engine.dispose()
    assert response.status_code == 200
    body = response.json()
    assert body["database"] == "ok"
    assert body["migration_head"] == HEAD
    assert body["setting_defaults"] == {}
