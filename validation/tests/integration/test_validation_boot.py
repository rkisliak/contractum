"""Validation boots on its own database with its own migration history (PLAT-05)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from dc_validation.main import create_validation_app
from dc_validation.settings import ValidationSettings
from psycopg.rows import dict_row

pytestmark = pytest.mark.integration


def _tables(db_url: str) -> set[str]:
    url = db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    return {row["tablename"] for row in rows}


def _indexes(db_url: str) -> set[str]:
    url = db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
        ).fetchall()
    return {row["indexname"] for row in rows}


def test_upgrade_creates_the_outbox_the_rule_set_tables_and_the_validation_version_table(
    fresh_db_url: str, migrate_validation: Callable[..., None]
) -> None:
    migrate_validation("upgrade")
    assert _tables(fresh_db_url) == {
        "outbox",
        "alembic_version_validation",
        "rule_set",
        "rule_set_version",
        "validation_run",
    }
    assert "ix_outbox_unpublished" in _indexes(fresh_db_url)


def test_check_reports_no_drift_between_models_and_migration(
    migrate_validation: Callable[..., None],
) -> None:
    migrate_validation("upgrade")
    migrate_validation("check")  # raises CommandError on drift


async def test_health_and_readiness_are_200_once_migrated(
    make_validation: Callable[..., Any], asgi_client: Any
) -> None:
    env = make_validation()
    async with asgi_client(env.app) as client:
        health = await client.get("/healthz")
        ready = await client.get("/readyz")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 200


async def test_readiness_is_a_generic_503_problem_when_the_database_is_unreachable(
    asgi_client: Any,
) -> None:
    app = create_validation_app(
        ValidationSettings(database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/never")  # type: ignore[arg-type]
    )
    async with asgi_client(app) as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "127.0.0.1" not in resp.text
    assert "postgres" not in resp.text.lower()


def _is_problem(resp: Any, status: int) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = resp.json()
    assert body["status"] == status
    assert body["type"].startswith("urn:dc:problem:")
    return body


async def test_unknown_validation_path_is_a_404_problem(
    make_validation: Callable[..., Any], asgi_client: Any
) -> None:
    env = make_validation()
    async with asgi_client(env.app) as client:
        body = _is_problem(await client.get("/api/v1/validation/anything"), 404)
    assert body["type"] == "urn:dc:problem:not-found"


async def test_wrong_method_on_healthz_is_a_405_problem(
    make_validation: Callable[..., Any], asgi_client: Any
) -> None:
    env = make_validation()
    async with asgi_client(env.app) as client:
        body = _is_problem(await client.post("/healthz"), 405)
    assert body["type"] == "urn:dc:problem:method-not-allowed"


def test_downgrade_to_base_drops_the_outbox_and_upgrade_recreates_it_without_drift(
    fresh_db_url: str, migrate_validation: Callable[..., None]
) -> None:
    migrate_validation("upgrade")
    migrate_validation("downgrade", "base")
    assert "outbox" not in _tables(fresh_db_url)
    assert "ix_outbox_unpublished" not in _indexes(fresh_db_url)
    migrate_validation("upgrade")
    assert "outbox" in _tables(fresh_db_url)
    assert "ix_outbox_unpublished" in _indexes(fresh_db_url)
    migrate_validation("check")
