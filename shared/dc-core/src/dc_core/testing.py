"""Shared pytest plugin: real Postgres 18, a database per test, an ASGI client.

Loaded through the root ``conftest.py``. Integration tests need a running Docker daemon;
unit tests never request these fixtures. ``release_library_caches`` keeps the session's heap
from growing with every test (the root ``conftest.py`` calls it after each one).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

NATS_IMAGE = "nats:2.15.3-alpine"


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:18", driver=None) as container:
        yield container


@pytest.fixture(scope="session")
def pg_admin_url(pg_container: PostgresContainer) -> str:
    """libpq-style URL (``postgresql://...``) for the cluster's default database."""
    return pg_container.get_connection_url()


@pytest.fixture
def fresh_db_url(pg_admin_url: str) -> Iterator[str]:
    """SQLAlchemy URL (``postgresql+psycopg://``) for a database created for this test only."""
    name = f"t_{uuid.uuid4().hex}"
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_url(pg_admin_url).set(drivername="postgresql+psycopg", database=name)
    try:
        yield url.render_as_string(hide_password=False)
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def asgi_client() -> Callable[..., AbstractAsyncContextManager[httpx.AsyncClient]]:
    """Factory: ``async with asgi_client(app) as client``. Runs the app lifespan.

    Starlette's TestClient is deprecated in 1.x, so every test goes through ASGITransport.
    ``raise_app_exceptions=False`` lets a test observe the 500 problem response.
    """

    @asynccontextmanager
    async def _make(
        app: FastAPI,
        *,
        raise_app_exceptions: bool = True,
        base_url: str = "http://testserver",
        **client_kwargs: Any,
    ) -> AsyncIterator[httpx.AsyncClient]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
            async with httpx.AsyncClient(
                transport=transport, base_url=base_url, **client_kwargs
            ) as client:
                yield client

    return _make


def release_library_caches() -> None:
    """Release what process-wide library caches still hold of the apps and engines a test built.

    A service process builds one app and one engine for its whole life, so these caches hold a
    fixed set there. A test session builds new ones in almost every test, and three library caches
    keep them reachable after the test has ended:

    * FastAPI's dependency classification caches in ``fastapi.dependencies.models`` (up to 4096
      callables each, keyed by the callable): every app's dependency closures, and through them
      its session factory, its engine and the statements the engine compiled;
    * every ORM mapper's compiled-statement cache (keyed by the dialect): every engine's dialect
      and the INSERT and UPDATE statements compiled for it;
    * the PostgreSQL dialect's reflection-query caches (``functools.lru_cache`` on its methods,
      keyed by the dialect instance): the dialects of the Alembic runs.

    Left alone they kept about 14,000 objects per test that starts the three services, so the
    integration session grew to millions of objects, and a full garbage collection of that heap
    held the GIL for up to 3.7 s inside requests that run under a deadline. Clearing a cache drops
    only entries that the next call rebuilds; the code under test is unchanged. The root
    ``conftest.py`` calls this after every test.
    """
    from fastapi.dependencies import models as fastapi_dependency_models
    from sqlalchemy.dialects.postgresql.base import PGDialect
    from sqlalchemy.orm import mapperlib

    for value in vars(fastapi_dependency_models).values():
        _cache_clear(value)
    for registry in mapperlib._all_registries():
        for mapper in registry.mappers:
            mapper._compiled_cache.clear()
    for klass in PGDialect.__mro__:
        for value in vars(klass).values():
            _cache_clear(value)


def _cache_clear(value: object) -> None:
    """Empty ``value`` if it is a ``functools`` cache (anything with a ``cache_clear``)."""
    clear = getattr(value, "cache_clear", None)
    if callable(clear):
        clear()


def _nats_container() -> DockerContainer:
    container = (
        DockerContainer(NATS_IMAGE)
        .with_command("-js -m 8222")
        .with_exposed_ports(4222, 8222)
        .waiting_for(LogMessageWaitStrategy("Server is ready").with_startup_timeout(60))
    )
    container.start()
    return container


def _nats_address(container: DockerContainer) -> str:
    host = container.get_container_host_ip()
    return f"nats://{host}:{container.get_exposed_port(4222)}"


@pytest.fixture(scope="session")
def nats_container() -> Iterator[DockerContainer]:
    """One JetStream-enabled NATS 2.15 server for the whole session."""
    container = _nats_container()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture
async def nats_url(nats_container: DockerContainer) -> AsyncIterator[str]:
    """URL of the session NATS server with clean ``AUDIT`` and ``ACCESS`` streams for this test.

    Tests share one server, so the streams (and their durable consumers) are deleted first: a
    message published by an earlier test must never reach this test's audit database.
    """
    import nats
    from nats.js.errors import NotFoundError

    url = _nats_address(nats_container)
    nc = await nats.connect(servers=[url])
    try:
        for stream in ("AUDIT", "ACCESS"):
            with contextlib.suppress(NotFoundError):
                await nc.jetstream().delete_stream(stream)
    finally:
        await nc.close()
    yield url


@dataclass
class NatsOutage:
    """A dedicated NATS server whose network can be frozen to simulate an outage."""

    url: str
    _container: DockerContainer

    def pause(self) -> None:
        self._container.get_wrapped_container().pause()

    def unpause(self) -> None:
        self._container.get_wrapped_container().unpause()


@pytest.fixture
def nats_outage() -> Iterator[NatsOutage]:
    """Function-scoped NATS container with ``.pause()`` / ``.unpause()`` (never the shared one)."""
    container = _nats_container()
    outage = NatsOutage(url=_nats_address(container), _container=container)
    try:
        yield outage
    finally:
        with contextlib.suppress(Exception):  # already running is fine
            outage.unpause()
        container.stop()
