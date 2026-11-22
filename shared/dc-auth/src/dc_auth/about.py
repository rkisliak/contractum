"""The internal ``GET /internal/v1/about`` route every service answers (quick 261120-0yc, D-14).

Identity's Settings page (P3) lists each service's version, commit, build time, migration head and
database state. The route is internal (never routed by the gateway) and gated by the ROOT
permission ``settings:read``, so only a caller holding it (the Global admin, with the caller's own
bearer) gets an answer; the usual policy dependency decides, like on every other route.

The migration head is read from the service's own Alembic version table with a 2 s timeout and
answers ``unavailable`` on any failure; a service with no database answers ``none``.
``setting_defaults`` carries the service's Helm/env defaults of the settings it owns (empty until
a later plan registers a runtime section there). Nothing secret is ever part of the answer.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from typing import Any, Literal

from dc_core.app import service_version
from dc_core.policy import PolicyRouter
from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dc_auth.deps import require

DB_TIMEOUT_S = 2.0
_TABLE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class ServiceAboutOut(BaseModel):
    """One service's build and database state, as seen from inside the service."""

    service: str
    version: str
    commit: str
    built_at: str
    database: Literal["ok", "unavailable", "none"]
    migration_head: str | None
    setting_defaults: dict[str, Any]


async def read_migration_head(
    session_factory: async_sessionmaker[AsyncSession] | None,
    version_table: str | None,
    *,
    timeout_s: float = DB_TIMEOUT_S,
) -> tuple[Literal["ok", "unavailable", "none"], str | None]:
    """``(database state, migration head)``; never raises."""
    if session_factory is None or version_table is None:
        return "none", None

    async def _read() -> str | None:
        async with session_factory() as session:
            row = (await session.execute(text(f"SELECT version_num FROM {version_table}"))).first()  # noqa: S608 - validated identifier
            return str(row[0]) if row is not None else None

    try:
        return "ok", await asyncio.wait_for(_read(), timeout_s)
    except Exception:  # noqa: BLE001 - any failure is "unavailable", never an error response
        return "unavailable", None


def about_router(
    service: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    version_table: str | None = None,
    setting_defaults: Mapping[str, Any] | None = None,
) -> APIRouter:
    """The ``/internal/v1/about`` router of ``service`` (include it in ``internal_routers``).

    ``session_factory`` says that the service has a database; the route then reads the app's own
    ``app.state.session_factory`` (set by ``dc_core.app.create_app``) at request time.
    """
    if version_table is not None and _TABLE.fullmatch(version_table) is None:
        raise ValueError("version_table must be a plain lowercase table name")
    router = PolicyRouter(prefix="/internal/v1")
    defaults = dict(setting_defaults or {})
    # The endpoint reads the app's own session factory per request and never closes over it:
    # FastAPI keeps endpoint callables in module-level caches (fastapi.dependencies.models) after
    # their app is gone, and a captured factory would keep its engine, connection pool and
    # compiled-statement cache alive with them.
    has_database = session_factory is not None

    async def get_about(request: Request) -> ServiceAboutOut:
        info = service_version(service)
        factory = getattr(request.app.state, "session_factory", None) if has_database else None
        database, head = await read_migration_head(factory, version_table)
        return ServiceAboutOut(
            service=service,
            version=info.version,
            commit=info.commit,
            built_at=info.built_at,
            database=database,
            migration_head=head,
            setting_defaults=defaults,
        )

    router.add_api_route(
        "/about",
        get_about,
        methods=["GET"],
        dependencies=[require("settings:read")],
        response_model=ServiceAboutOut,
        include_in_schema=False,
    )
    return router


__all__ = ["DB_TIMEOUT_S", "ServiceAboutOut", "about_router", "read_migration_head"]
