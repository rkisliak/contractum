"""The shared httpx client closes idle connections before the server does."""

from __future__ import annotations

import inspect
from typing import Any

import httpx
import pytest
import uvicorn
from dc_core.http import KEEPALIVE_EXPIRY_S, make_client

pytestmark = pytest.mark.unit


def _pool(client: httpx.AsyncClient) -> Any:
    transport: Any = client._transport  # noqa: SLF001
    return transport._pool  # noqa: SLF001


def test_client_expires_idle_connections_before_uvicorn() -> None:
    server_idle_s = (
        inspect.signature(uvicorn.Config.__init__).parameters["timeout_keep_alive"].default
    )
    assert KEEPALIVE_EXPIRY_S < server_idle_s


@pytest.mark.asyncio
async def test_make_client_applies_the_limits_and_keeps_pool_sizes() -> None:
    async with make_client() as client:
        pool = _pool(client)
        assert pool._keepalive_expiry == KEEPALIVE_EXPIRY_S  # noqa: SLF001
        assert pool._max_connections == 100  # noqa: SLF001
        assert pool._max_keepalive_connections == 20  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_caller_can_still_set_its_own_limits() -> None:
    limits = httpx.Limits(max_connections=7, keepalive_expiry=1.0)
    async with make_client(limits=limits) as client:
        pool = _pool(client)
        assert pool._max_connections == 7  # noqa: SLF001
        assert pool._keepalive_expiry == 1.0  # noqa: SLF001
