"""CR-EXTRA (04.1 review fix): the delivery worker frees its session advisory lock when it stops.

Closing a pooled engine connection only returns the Postgres backend to the pool, where the session
lock would stay held and no other replica could ever take over. ``CardNotifier`` unlocks explicitly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from dc_contracts.advisory_lock import close_session_lock, try_session_lock
from dc_contracts.delivery import LOCK_KEY, CardNotifier
from sqlalchemy import text

pytestmark = pytest.mark.integration

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]


async def lock_is_free(engine: Any) -> bool:
    async with engine.connect() as probe:
        got = (
            await probe.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": LOCK_KEY}
            )
        ).scalar_one()
        if got:
            await probe.execute(
                text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": LOCK_KEY}
            )
        await probe.commit()
    return bool(got)


async def test_cancelling_the_notifier_releases_the_advisory_lock_for_the_next_replica(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        state = stack.contracts_app.state
        engine = state.session_factory.kw["bind"]
        settings = state.settings.model_copy(update={"notify_poll_interval_s": 0.2})
        notifier = CardNotifier(state.session_factory, settings)

        task = asyncio.create_task(notifier.run())
        try:
            # The loop holds the lock while it runs.
            for _ in range(50):
                if not await lock_is_free(engine):
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("the notifier never took the advisory lock")
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await notifier.aclose()

        assert await lock_is_free(engine), "the cancelled notifier still holds its advisory lock"


async def test_try_session_lock_is_exclusive_and_the_loser_connection_is_closed(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        factory = stack.contracts_app.state.session_factory
        engine = factory.kw["bind"]
        key = "dc-test-session-lock"

        first = await try_session_lock(factory, key, "Test")
        assert first is not None
        try:
            # A second holder gets None, and the connection it opened is given back (not leaked).
            checked_out = engine.pool.checkedout()
            assert await try_session_lock(factory, key, "Test") is None
            assert engine.pool.checkedout() == checked_out
        finally:
            await close_session_lock(first, key, "test_unlock_failed")

        again = await try_session_lock(factory, key, "Test")
        assert again is not None, "the lock was not freed by close_session_lock"
        await close_session_lock(again, key, "test_unlock_failed")
        await close_session_lock(None, key, "test_unlock_failed")


async def test_try_session_lock_names_the_owner_when_the_factory_is_unbound() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    with pytest.raises(RuntimeError, match="CardNotifier needs a session factory"):
        await try_session_lock(async_sessionmaker(), "dc-test-session-lock", "CardNotifier")
