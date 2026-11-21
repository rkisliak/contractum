"""OutboxRelay guards that need no database: the engine check and the lock connection cleanup."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from dc_core.events import PublishAck
from dc_core.relay import OutboxRelay

pytestmark = pytest.mark.unit


class _NoPublisher:
    async def publish(self, subject: str, payload: bytes, *, msg_id: str) -> PublishAck:
        raise AssertionError("nothing may be published")

    async def close(self) -> None:
        return None


class _Conn:
    """A lock connection whose statement or close can fail."""

    def __init__(self, *, fail_execute: bool = False, fail_close: bool = False) -> None:
        self.fail_execute = fail_execute
        self.fail_close = fail_close
        self.closed = 0
        self.invalidated = 0

    async def invalidate(self) -> None:
        self.invalidated += 1

    async def execution_options(self, **_: Any) -> _Conn:
        return self

    async def execute(self, *_: Any, **__: Any) -> Any:
        if self.fail_execute:
            raise ConnectionResetError("server closed the connection")
        raise AssertionError("not expected")

    async def close(self) -> None:
        self.closed += 1
        if self.fail_close:
            raise ConnectionResetError("already closed")


def _relay(bind: Any) -> OutboxRelay:
    factory = SimpleNamespace(kw={} if bind is None else {"bind": bind})
    return OutboxRelay(factory, object(), service="probe", publisher=_NoPublisher())  # type: ignore[arg-type]


async def test_the_lock_needs_a_session_factory_bound_to_an_engine() -> None:
    with pytest.raises(RuntimeError, match="bound to an engine"):
        await _relay(None)._try_lock()


async def test_a_failed_lock_query_closes_its_connection_and_propagates() -> None:
    conn = _Conn(fail_execute=True)

    class _Engine:
        async def connect(self) -> _Conn:
            return conn

    with pytest.raises(ConnectionResetError):
        await _relay(_Engine())._try_lock()
    assert conn.closed == 1


async def test_releasing_the_lock_is_best_effort() -> None:
    conn = _Conn(fail_close=True)
    with structlog.testing.capture_logs() as captured:
        await OutboxRelay._close_lock(conn)  # type: ignore[arg-type]
        await OutboxRelay._close_lock(None)
    assert (conn.invalidated, conn.closed) == (1, 1)
    assert [(e["event"], e["error"]) for e in captured] == [
        ("outbox_relay_unlock_failed", "ConnectionResetError")
    ]
