"""SUB-07 / D-96 (04.1-16 gap): the sunset scheduler's background ``run()`` loop itself.

``test_sunset_notices.py`` drives ``scan_once`` by hand. These tests start ``run()`` as a task and
check what the loop does on its own: it scans without help, it yields to a replica holding the
session advisory lock and takes over when that lock is freed, and cancelling it releases the lock
so the next replica can take it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from dc_contracts.scheduler import LOCK_KEY, SunsetScheduler
from sqlalchemy import text

pytestmark = pytest.mark.integration

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

CARDS = "/api/v1/contracts/cards"
SUNSET_EVENT = "contract.version.sunset_approaching"
LOOP_INTERVAL_S = 1


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def today_plus(days: int) -> date:
    return (datetime.now(UTC) + timedelta(days=days)).date()


async def pinned_world(stack: Any) -> dict[str, Any]:
    """A card pinning 2.3.0, which is deprecated with a sunset 30 days away by the caller."""
    chain = await stack.chain()
    contract, _versions = await stack.publish_versions(chain.team, ["2.3.0", "3.0.0"])
    card = await stack.create_card(chain.team, name=f"loop {uuid.uuid4().hex[:6]}")
    pinned = await stack.subscribe(card["id"], contract, "2.3.0")
    assert pinned.status_code == 201, pinned.text
    return {"contract": contract, "card": card, "pin": pinned.json()["id"]}


async def deprecate_in_30_days(stack: Any, contract: str) -> None:
    response = await stack.client.post(
        f"/api/v1/contracts/contracts/{contract}/versions/2.3.0/deprecate",
        json={"sunset_date": today_plus(30).isoformat()},
        headers=bearer(stack.admin_token),
    )
    assert response.status_code == 200, response.text


def sunset_notices(sql: Sql, stack: Any, card_id: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT * FROM card_notice WHERE card_id = %s AND event_type = %s ORDER BY id",
        card_id,
        SUNSET_EVENT,
    )


def fast_scheduler(stack: Any) -> SunsetScheduler:
    """The app's scheduler over the same session factory, with a one-second loop interval."""
    state = stack.contracts_app.state
    settings = state.settings.model_copy(update={"sunset_scan_interval_s": LOOP_INTERVAL_S})
    return SunsetScheduler(state.session_factory, settings)


async def wait_until(probe: Callable[[], bool], *, within_s: float = 20.0) -> None:
    deadline = time.monotonic() + within_s
    while not probe():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"condition not met within {within_s} s")
        await asyncio.sleep(0.2)


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_the_loop_records_the_notice_without_anyone_calling_scan_once(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await pinned_world(stack)
        await deprecate_in_30_days(stack, w["contract"])
        task = asyncio.create_task(fast_scheduler(stack).run())
        try:
            await wait_until(lambda: len(sunset_notices(sql, stack, w["card"]["id"])) >= 1)
            # Several more loop passes over the same pin add nothing (the dedupe key holds).
            await asyncio.sleep(LOOP_INTERVAL_S * 3)
        finally:
            await stop(task)

        notices = sunset_notices(sql, stack, w["card"]["id"])
        assert len(notices) == 1
        assert notices[0]["payload"]["lead_days"] == 30
        assert notices[0]["dedupe_key"] == f"sunset:{w['pin']}:30:{today_plus(30).isoformat()}"
        assert task.cancelled()


async def test_the_loop_waits_while_another_replica_holds_the_lock_and_takes_over_when_freed(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await pinned_world(stack)
        await deprecate_in_30_days(stack, w["contract"])
        engine = stack.contracts_app.state.session_factory.kw["bind"]

        async with engine.connect() as holder:
            await holder.execute(text("SELECT pg_advisory_lock(hashtext(:key))"), {"key": LOCK_KEY})
            await holder.commit()
            task = asyncio.create_task(fast_scheduler(stack).run())
            try:
                # Several intervals pass while the other replica holds the lock: no scan at all.
                await asyncio.sleep(LOOP_INTERVAL_S * 3)
                assert sunset_notices(sql, stack, w["card"]["id"]) == []

                await holder.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": LOCK_KEY}
                )
                await holder.commit()
                await wait_until(lambda: len(sunset_notices(sql, stack, w["card"]["id"])) == 1)
            finally:
                await stop(task)


async def test_cancelling_the_loop_releases_the_advisory_lock_for_the_next_replica(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await pinned_world(stack)
        await deprecate_in_30_days(stack, w["contract"])
        engine = stack.contracts_app.state.session_factory.kw["bind"]

        task = asyncio.create_task(fast_scheduler(stack).run())
        try:
            await wait_until(lambda: len(sunset_notices(sql, stack, w["card"]["id"])) >= 1)
        finally:
            await stop(task)

        # The lock must be free the moment the loop is cancelled, from a fresh connection.
        async with engine.connect() as probe:
            got = (
                await probe.execute(
                    text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": LOCK_KEY}
                )
            ).scalar_one()
            await probe.execute(
                text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": LOCK_KEY}
            )
            await probe.commit()
        assert got is True, "the cancelled loop still holds the sunset advisory lock"
