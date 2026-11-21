"""Health routes, lifespan hooks and engine disposal of ``create_app`` (no database needed)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from dc_core.app import create_app
from dc_core.db import make_async_engine, make_session_factory
from dc_core.settings import CoreSettings
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.unit

# Nothing listens on port 1: every connection attempt is refused at once.
UNREACHABLE_DB = "postgresql+psycopg://probe@localhost:1/probe"


def _app(**kwargs: Any) -> FastAPI:
    return create_app(
        service="probe",
        settings=CoreSettings(service="probe", database_url=UNREACHABLE_DB),
        routers=[],
        **kwargs,
    )


async def test_healthz_answers_without_a_database(asgi_client: Any) -> None:
    async with asgi_client(_app()) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_is_ready_when_the_service_has_no_database(asgi_client: Any) -> None:
    async with asgi_client(_app()) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_is_a_503_problem_when_the_database_is_unreachable(asgi_client: Any) -> None:
    factory = make_session_factory(make_async_engine(UNREACHABLE_DB))
    async with asgi_client(_app(session_factory=factory)) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "urn:dc:problem:service-unavailable"
    assert body["detail"] == "The service is not ready."
    assert "localhost" not in response.text


async def test_lifespan_hooks_run_in_order_and_the_engine_is_disposed(
    asgi_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    def hook(name: str) -> Any:
        @asynccontextmanager
        async def _hook(app: FastAPI) -> AsyncIterator[None]:
            events.append(f"enter {name} {app.state.service}")
            try:
                yield
            finally:
                events.append(f"exit {name}")

        return _hook

    engine = make_async_engine(UNREACHABLE_DB)
    disposed: list[bool] = []
    original_dispose = AsyncEngine.dispose

    async def dispose(self: AsyncEngine, close: bool = True) -> None:
        disposed.append(self is engine)
        await original_dispose(self, close)

    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    app = _app(session_factory=make_session_factory(engine), lifespan_hooks=[hook("a"), hook("b")])
    async with asgi_client(app):
        assert events == ["enter a probe", "enter b probe"]
    assert events == ["enter a probe", "enter b probe", "exit b", "exit a"]
    assert disposed == [True]
