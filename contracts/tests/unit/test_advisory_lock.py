"""The session advisory lock helpers' failure paths (fake connections, no Postgres).

``tests/integration/test_notifier_lock.py`` proves the lock against a real server; these tests
pin what happens when the lock is held elsewhere or a statement fails: the connection is always
closed, and a backend whose unlock failed is invalidated so the server frees the lock.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from dc_contracts.advisory_lock import close_session_lock, release_session_lock, try_session_lock
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value


class _Conn:
    def __init__(self, *, got: bool = True, fail: BaseException | None = None) -> None:
        self.got = got
        self.fail = fail
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.options: dict[str, Any] = {}
        self.closed = False
        self.invalidated = False

    async def execution_options(self, **options: Any) -> _Conn:
        self.options.update(options)
        return self

    async def execute(self, statement: Any, params: dict[str, Any]) -> _Result:
        self.statements.append((str(statement), params))
        if self.fail is not None:
            raise self.fail
        return _Result(self.got)

    async def invalidate(self) -> None:
        self.invalidated = True

    async def close(self) -> None:
        self.closed = True


class _Engine:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def connect(self) -> _Conn:
        return self.conn


def _factory(conn: _Conn | None) -> Any:
    return SimpleNamespace(kw={} if conn is None else {"bind": _Engine(conn)})


async def test_an_unbound_session_factory_is_refused_with_the_owner_named() -> None:
    with pytest.raises(RuntimeError, match="CardNotifier needs a session factory"):
        await try_session_lock(_factory(None), "k", "CardNotifier")


async def test_the_lock_is_taken_on_an_autocommit_connection_that_stays_open() -> None:
    conn = _Conn(got=True)
    held = await try_session_lock(_factory(conn), "card-notifier", "CardNotifier")
    assert held is cast(Any, conn)
    assert conn.options == {"isolation_level": "AUTOCOMMIT"}
    assert conn.statements == [
        ("SELECT pg_try_advisory_lock(hashtext(:key))", {"key": "card-notifier"})
    ]
    assert not conn.closed


async def test_a_lock_held_elsewhere_returns_none_and_closes_the_connection() -> None:
    conn = _Conn(got=False)
    assert await try_session_lock(_factory(conn), "k", "owner") is None
    assert conn.closed


async def test_a_failed_lock_statement_closes_the_connection_and_raises() -> None:
    conn = _Conn(fail=ConnectionError("gone"))
    with pytest.raises(ConnectionError):
        await try_session_lock(_factory(conn), "k", "owner")
    assert conn.closed


async def test_release_unlocks_and_returns_the_connection_without_invalidating() -> None:
    conn = _Conn()
    await release_session_lock(cast(Any, conn), "k")
    assert conn.statements == [("SELECT pg_advisory_unlock(hashtext(:key))", {"key": "k"})]
    assert conn.closed
    assert not conn.invalidated


async def test_a_failed_unlock_invalidates_the_backend_closes_and_raises() -> None:
    conn = _Conn(fail=TimeoutError())
    with pytest.raises(TimeoutError):
        await release_session_lock(cast(Any, conn), "k")
    assert conn.invalidated
    assert conn.closed


async def test_closing_no_lock_does_nothing() -> None:
    with capture_logs() as logs:
        await close_session_lock(None, "k", "unused_event")
    assert logs == []


async def test_a_failed_release_is_logged_under_the_callers_event_and_not_raised() -> None:
    conn = _Conn(fail=OSError("reset"))
    with capture_logs() as logs:
        await close_session_lock(cast(Any, conn), "k", "card_notifier_unlock_failed")
    assert conn.invalidated and conn.closed
    assert logs == [
        {"event": "card_notifier_unlock_failed", "error": "OSError", "log_level": "warning"}
    ]
