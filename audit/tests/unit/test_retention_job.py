"""Retention job paths that need no database: an unbound factory, a failed lock, the loop."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from dc_audit import retention as retention_module
from dc_audit.retention import RetentionJob, RetentionResult
from dc_audit.settings import AuditSettings
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit


class _Conn:
    """An autocommit connection stand-in whose lock query fails."""

    def __init__(self) -> None:
        self.closed = False

    async def execution_options(self, **options: Any) -> _Conn:
        assert options == {"isolation_level": "AUTOCOMMIT"}
        return self

    async def execute(self, *_args: Any) -> Any:
        raise ConnectionResetError("server closed the connection")

    async def close(self) -> None:
        self.closed = True


def _job(bind: Any = None, **settings: Any) -> RetentionJob:
    factory = SimpleNamespace(kw={} if bind is None else {"bind": bind})
    return RetentionJob(cast(Any, factory), AuditSettings(**settings))


async def test_a_session_factory_without_an_engine_is_refused() -> None:
    with pytest.raises(RuntimeError, match="bound to an engine"):
        await _job().run_once()


async def test_a_failed_lock_query_closes_the_connection_and_raises() -> None:
    conn = _Conn()

    async def _connect() -> _Conn:
        return conn

    with pytest.raises(ConnectionResetError):
        await _job(SimpleNamespace(connect=_connect)).run_once()
    assert conn.closed is True


def _scripted(job: RetentionJob, outcomes: Iterable[Any]) -> None:
    """Replace ``run_once`` with a script; exceptions in it are raised."""
    script = iter(outcomes)

    async def _run_once(*, now: Any = None) -> RetentionResult | None:
        outcome = next(script)
        if isinstance(outcome, BaseException):
            raise outcome
        return cast(RetentionResult | None, outcome)

    job.run_once = _run_once


async def test_the_loop_logs_counts_survives_errors_and_stops_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(
        retention_module,
        "asyncio",
        SimpleNamespace(sleep=_sleep, CancelledError=asyncio.CancelledError),
    )
    job = _job(retention_interval_s=600)
    _scripted(
        job,
        [
            RetentionResult(days=120, deleted=7, batches=1),
            RuntimeError("database down"),
            RetentionResult(days=None, deleted=0, batches=0),  # nothing deleted: nothing logged
            None,  # another replica holds the lock
            asyncio.CancelledError(),
        ],
    )
    with capture_logs() as logs, pytest.raises(asyncio.CancelledError):
        await job.run()
    assert sleeps == [30.0, 600.0, 600.0, 600.0, 600.0]  # first pass after at most 30 s
    assert logs == [
        {
            "event": "audit_retention_applied",
            "days": 120,
            "deleted": 7,
            "batches": 1,
            "log_level": "info",
        },
        {"event": "audit_retention_failed", "error": "RuntimeError", "log_level": "warning"},
    ]
