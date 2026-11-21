"""The internal ``/internal/v1/about`` route factory without a database (quick 261120-0yc)."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from dc_auth.about import about_router, read_migration_head
from dc_auth.authorizer import Outcome
from dc_auth.testing import FakeIssuer
from dc_core.app import BUILD_COMMIT_ENV, BUILD_TIME_ENV

pytestmark = pytest.mark.unit

ABOUT = "/internal/v1/about"


@pytest.mark.parametrize(
    "version_table",
    ["Alembic_Version", "1version", "version;drop table x", "a" * 64, ""],
    ids=["upper-case", "leading-digit", "injection", "too-long", "empty"],
)
def test_a_version_table_that_is_not_a_plain_name_is_refused(version_table: str) -> None:
    with pytest.raises(ValueError, match="plain lowercase table name"):
        about_router("probe", version_table=version_table)


async def test_without_a_session_factory_or_table_the_database_is_none() -> None:
    assert await read_migration_head(None, "probe_version") == ("none", None)
    assert await read_migration_head(None, None) == ("none", None)


async def test_the_route_answers_build_info_and_defaults_for_an_allowed_caller(
    fake_issuer: FakeIssuer,
    asgi_client: Any,
    probe_app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BUILD_COMMIT_ENV, "abc1234")
    monkeypatch.delenv(BUILD_TIME_ENV, raising=False)
    router = about_router("probe", setting_defaults={"banner": {"enabled": False}})
    headers = {"Authorization": f"Bearer {fake_issuer.mint('admin')}"}
    async with asgi_client(probe_app(internal_routers=[router])) as client:
        response = await client.get(ABOUT, headers=headers)
        anonymous = await client.get(ABOUT)
    assert response.status_code == 200
    assert response.json() == {
        "service": "probe",
        "version": "dev",  # no installed dc-probe package
        "commit": "abc1234",
        "built_at": "dev",
        "database": "none",
        "migration_head": None,
        "setting_defaults": {"banner": {"enabled": False}},
    }
    assert anonymous.status_code == 401
    assert anonymous.json()["type"] == "urn:dc:problem:unauthenticated"


async def test_a_caller_without_settings_read_gets_no_build_info(
    fake_issuer: FakeIssuer, asgi_client: Any, probe_app: Any
) -> None:
    app = probe_app(internal_routers=[about_router("probe")], outcome=Outcome.FORBIDDEN)
    headers = {"Authorization": f"Bearer {fake_issuer.mint('reader')}"}
    async with asgi_client(app) as client:
        response = await client.get(ABOUT, headers=headers)
    assert response.status_code == 403
    assert response.json()["type"] == "urn:dc:problem:forbidden"
    assert "commit" not in response.json()


class _HangingSession:
    async def __aenter__(self) -> _HangingSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def execute(self, statement: object) -> None:
        await asyncio.Event().wait()  # a database that never answers


async def test_a_database_that_does_not_answer_in_time_is_unavailable() -> None:
    state = await read_migration_head(cast(Any, _HangingSession), "probe_version", timeout_s=0.01)
    assert state == ("unavailable", None)
