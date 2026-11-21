"""The background loops of the card notifier and the sunset scheduler, with no database.

The lock, the pass and ``asyncio.sleep`` are replaced, so each test walks the loop through an exact
sequence: a dead lock connection is closed and the lock taken again, a failing pass is logged by
class and backed off, and cancelling the loop releases whatever lock it holds.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from dc_contracts import delivery, scheduler
from dc_contracts.delivery import CardNotifier
from dc_contracts.scheduler import SunsetScheduler
from dc_contracts.settings import ContractsSettings
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit

DB = "postgresql+psycopg://nobody:nobody@127.0.0.1:1/never"


def _settings(**overrides: Any) -> ContractsSettings:
    return ContractsSettings(database_url=DB, **overrides)  # type: ignore[arg-type]


class _LockConn:
    """A lock connection; ``dead`` makes its liveness probe fail like a dropped socket."""

    def __init__(self, name: str, *, dead: bool = False) -> None:
        self.name = name
        self.dead = dead

    async def execute(self, _statement: Any) -> None:
        if self.dead:
            raise ConnectionError("lock connection dropped")


class _Script:
    """Hands out lock connections in order, records closes, and ends the loop after N sleeps."""

    def __init__(self, locks: list[_LockConn | None], sleeps_before_cancel: int) -> None:
        self._locks = locks
        self.closed: list[str | None] = []
        self.sleeps: list[float] = []
        self._limit = sleeps_before_cancel

    async def try_lock(self) -> _LockConn | None:
        return self._locks.pop(0)

    async def close_lock(self, conn: _LockConn | None) -> None:
        self.closed.append(conn.name if conn is not None else None)

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        if len(self.sleeps) >= self._limit:
            raise asyncio.CancelledError


def _wire(monkeypatch: pytest.MonkeyPatch, module: Any, worker: Any, script: _Script) -> None:
    monkeypatch.setattr(worker, "_try_lock", script.try_lock)
    monkeypatch.setattr(worker, "_close_lock", script.close_lock)
    monkeypatch.setattr(module.asyncio, "sleep", script.sleep)


async def test_the_scheduler_retakes_a_dropped_lock_and_survives_a_failing_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _LockConn("first")
    worker = SunsetScheduler(None, _settings(sunset_scan_interval_s=7))  # type: ignore[arg-type]
    script = _Script([first, _LockConn("second")], sleeps_before_cancel=2)
    _wire(monkeypatch, scheduler, worker, script)
    outcomes: list[Any] = [RuntimeError("database down"), 3]

    async def scan_once() -> int:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            # The scan failed and, meanwhile, the lock connection died with the database.
            first.dead = True
            raise outcome
        return int(outcome)

    monkeypatch.setattr(worker, "scan_once", scan_once)
    with capture_logs() as logs, pytest.raises(asyncio.CancelledError):
        await worker.run()

    assert script.sleeps == [7.0, 7.0]
    # The dead connection is closed before the lock is taken again; the loop's own finally
    # releases the second one when it is cancelled.
    assert script.closed == ["first", "second"]
    assert [(e["event"], e.get("error"), e.get("notices")) for e in logs] == [
        ("sunset_scan_failed", "RuntimeError", None),
        ("sunset_scan_done", None, 3),
    ]


async def test_the_notifier_waits_for_the_lock_backs_off_on_errors_and_resets_after_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = _LockConn("held")
    worker = CardNotifier(
        None,  # type: ignore[arg-type]
        _settings(notify_poll_interval_s=2.0, notify_batch_size=5),
        http_client=httpx.AsyncClient(),
    )
    script = _Script([None, held, _LockConn("retaken")], sleeps_before_cancel=4)
    _wire(monkeypatch, delivery, worker, script)
    passes: list[Any] = [5, ValueError("bad row"), ValueError("bad row"), 0]

    async def run_once() -> int:
        outcome = passes.pop(0)
        if isinstance(outcome, Exception):
            held.dead = True
            raise outcome
        return int(outcome)

    monkeypatch.setattr(worker, "run_once", run_once)
    with capture_logs() as logs, pytest.raises(asyncio.CancelledError):
        await worker.run()

    # No lock: wait one poll. A full batch: straight on, no wait. Two failures: 2 s then 4 s.
    # A short batch after a good pass: back to one poll.
    assert script.sleeps == [2.0, 2.0, 4.0, 2.0]
    assert script.closed == ["held", "retaken"]
    assert [e["error"] for e in logs if e["event"] == "card_notifier_loop_error"] == [
        "ValueError",
        "ValueError",
    ]
    assert passes == []


async def test_a_notifier_given_a_client_leaves_closing_it_to_its_owner() -> None:
    client = httpx.AsyncClient()
    worker = CardNotifier(None, _settings(), http_client=client)  # type: ignore[arg-type]
    await worker.aclose()
    assert client.is_closed is False
    await client.aclose()


def _claim(channel: str) -> Any:
    return delivery._Claim(
        delivery_id=uuid.uuid4(),
        channel=channel,
        attempts=0,
        notice=None,  # type: ignore[arg-type]
        card_name="c",
        channel_enabled=True,
        webhook_url=None,
        secret_enc=None,
        previous_enc=None,
        previous_valid_until=None,
        email_channel_on=False,
        contacts=[],
        card_archived=False,
    )


def _notifier(monkeypatch: pytest.MonkeyPatch, claims: list[Any]) -> CardNotifier:
    worker = CardNotifier(None, _settings(), http_client=httpx.AsyncClient())  # type: ignore[arg-type]

    async def claim_due() -> list[Any]:
        return claims

    monkeypatch.setattr(worker, "claim_due", claim_due)
    return worker


async def test_a_channel_with_no_sender_is_recorded_as_an_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _notifier(monkeypatch, [_claim("sms")])
    recorded: list[Any] = []

    async def record(claim: Any, result: Any, effective: Any) -> None:
        recorded.append((claim.channel, result.kind, result.error))

    monkeypatch.setattr(worker, "_record", record)
    assert await worker.run_once() == 1
    assert recorded == [("sms", "failed", "internal-error")]


async def test_cancelling_a_send_cancels_the_pass_and_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _notifier(monkeypatch, [_claim("webhook")])
    recorded: list[Any] = []

    async def cancelled(claim: Any, effective: Any) -> Any:
        raise asyncio.CancelledError

    async def record(*args: Any) -> None:
        recorded.append(args)

    monkeypatch.setattr(worker, "_attempt", cancelled)
    monkeypatch.setattr(worker, "_record", record)
    with pytest.raises(asyncio.CancelledError):
        await worker.run_once()
    assert recorded == []


async def test_cancelling_the_record_step_cancels_the_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _notifier(monkeypatch, [_claim("sms")])

    async def cancelled(*_args: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "_record", cancelled)
    with capture_logs() as logs, pytest.raises(asyncio.CancelledError):
        await worker.run_once()
    assert [e["event"] for e in logs] == ["card_notice_delivery_error"]


async def test_a_record_step_error_is_logged_and_the_pass_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = [_claim("sms"), _claim("sms")]
    worker = _notifier(monkeypatch, claims)

    async def failing(*_args: Any) -> None:
        raise ConnectionError("database down")

    monkeypatch.setattr(worker, "_record", failing)
    with capture_logs() as logs:
        assert await worker.run_once() == 2
    errors = [e for e in logs if e["event"] == "card_notice_record_error"]
    assert sorted(e["delivery_id"] for e in errors) == sorted(str(c.delivery_id) for c in claims)
    assert {e["error"] for e in errors} == {"ConnectionError"}


async def test_cancelling_the_scheduler_during_a_scan_releases_its_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = SunsetScheduler(None, _settings())  # type: ignore[arg-type]
    script = _Script([_LockConn("only")], sleeps_before_cancel=99)
    _wire(monkeypatch, scheduler, worker, script)

    async def cancelled() -> int:
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "scan_once", cancelled)
    with capture_logs() as logs, pytest.raises(asyncio.CancelledError):
        await worker.run()
    assert script.closed == ["only"]
    assert script.sleeps == []
    assert logs == []
