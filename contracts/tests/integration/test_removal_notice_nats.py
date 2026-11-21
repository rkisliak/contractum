"""SUB-06 / SUB-07 (04.1-10 gap): a pin removal reaches the card over live NATS.

``test_subscription_removal.py`` proves the removal's handler through ``Stack.pump_notices``. This
test drives the real path: the removal writes its outbox row, the outbox relay publishes it to the
AUDIT stream, and the in-service ``contracts-notices`` consumer materialises the card's notice.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import nats
import pytest
from nats.js.api import StorageType, StreamConfig

pytestmark = pytest.mark.integration

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]
REASON = "Retiring 2.3.0, please move to 3.0.0"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def eventually(probe: Callable[[], Awaitable[Any]], *, within_s: float = 20.0) -> Any:
    deadline = time.monotonic() + within_s
    while True:
        value = await probe()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(f"condition not met within {within_s} s")
        await asyncio.sleep(0.2)


async def create_audit_stream(url: str) -> None:
    """Create stream AUDIT as the audit service does (the contracts service never does)."""
    nc = await nats.connect(servers=[url])
    try:
        await nc.jetstream().add_stream(
            StreamConfig(
                name="AUDIT",
                subjects=["audit.>"],
                storage=StorageType.FILE,
                duplicate_window=120,
            )
        )
    finally:
        await nc.close()


@pytest.fixture
async def audit_stream(nats_url: str) -> AsyncIterator[str]:
    await create_audit_stream(nats_url)
    yield nats_url


async def test_a_removal_published_by_the_relay_becomes_the_card_notice_over_nats(
    make_stack: MakeStack, audit_stream: str, sql: Sql
) -> None:
    async with make_stack(nats_url=audit_stream) as stack:
        chain = await stack.chain()
        contract, versions = await stack.publish_versions(chain.team, ["2.3.0", "3.0.0"])
        card = await stack.create_card(chain.team)
        pinned = await stack.subscribe(card["id"], contract, "2.3.0")
        assert pinned.status_code == 201, pinned.text
        pin_id = pinned.json()["id"]

        removed = await stack.client.post(
            f"/api/v1/contracts/contracts/{contract}/subscriptions/{pin_id}/remove",
            json={"reason": REASON},
            headers=bearer(stack.admin_token),
        )
        assert removed.status_code == 200, removed.text

        async def _removal_notices() -> list[dict[str, Any]]:
            rows = sql(
                stack.contracts_db_url,
                "SELECT * FROM card_notice WHERE card_id = %s ORDER BY id",
                card["id"],
            )
            return [n for n in rows if n["event_type"] == "card.subscription.removed"]

        found = await eventually(_removal_notices)
        (outbox,) = sql(
            stack.contracts_db_url,
            "SELECT event_id FROM outbox WHERE action = 'card.subscription_removed'",
        )
        assert len(found) == 1
        assert str(found[0]["source_event_id"]) == str(outbox["event_id"])
        assert str(found[0]["version_id"]) == versions["2.3.0"]
