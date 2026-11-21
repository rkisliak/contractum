"""The access-event queue (D-171, D-185): bounded, drop-oldest, batched, never blocking a read."""

from __future__ import annotations

import asyncio
import inspect
import itertools
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import structlog
from dc_core.access import AccessLog, access_log_hook
from dc_core.events import PublishAck
from dc_core.settings import CoreSettings
from fastapi import FastAPI

pytestmark = pytest.mark.unit


def envelope(n: int) -> dict[str, Any]:
    return {"event_id": f"evt-{n}", "n": n}


class Recording:
    """Records every publish with the queue length seen at that moment (reveals batch borders)."""

    def __init__(self) -> None:
        self.log: AccessLog | None = None
        self.calls: list[tuple[str, str, int]] = []
        self.fail_next = 0
        self.attempts = 0

    async def publish(self, subject: str, payload: bytes, *, msg_id: str) -> PublishAck:
        self.attempts += 1
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("broker down")
        pending = self.log.pending if self.log is not None else -1
        self.calls.append((subject, msg_id, pending))
        return PublishAck(duplicate=False)

    async def close(self) -> None:
        return None


class Hanging:
    """A publisher whose publish never returns."""

    def __init__(self) -> None:
        self.started = 0

    async def publish(self, subject: str, payload: bytes, *, msg_id: str) -> PublishAck:
        self.started += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def close(self) -> None:
        return None


@asynccontextmanager
async def running(log: AccessLog) -> AsyncIterator[AccessLog]:
    task = asyncio.create_task(log.run())
    try:
        yield log
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def wait_until(predicate: Any, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def test_a_full_queue_drops_the_oldest_and_counts_each_drop() -> None:
    publisher = Recording()
    log = AccessLog(publisher, capacity=3, batch=100, flush_s=0.05)
    for n in range(1, 6):
        log.record("access.contracts.contract.read", envelope(n))
    assert log.pending == 3
    assert log.dropped == 2
    assert log.dropped_by_reason == {"queue_full": 2}
    await log.flush(1.0)
    assert [msg_id for _, msg_id, _ in publisher.calls] == ["evt-3", "evt-4", "evt-5"]


async def test_exactly_capacity_events_drop_nothing() -> None:
    log = AccessLog(Recording(), capacity=3, batch=100, flush_s=0.05)
    for n in range(3):
        log.record("access.contracts.contract.read", envelope(n))
    assert log.pending == 3
    assert log.dropped == 0


def test_the_first_and_every_thousandth_drop_log_a_warning_with_the_counter() -> None:
    log = AccessLog(Recording(), capacity=1, batch=100, flush_s=1.0)
    with structlog.testing.capture_logs() as captured:
        for n in range(2001):
            log.record("access.contracts.contract.read", envelope(n))
    assert log.dropped == 2000
    warnings = [entry for entry in captured if entry["event"] == "access_events_dropped"]
    assert [(w["dropped"], w["reason"]) for w in warnings] == [
        (1, "queue_full"),
        (1000, "queue_full"),
        (2000, "queue_full"),
    ]
    assert all(w["log_level"] == "warning" for w in warnings)


def test_record_is_synchronous_constant_time_and_never_raises() -> None:
    assert not inspect.iscoroutinefunction(AccessLog.record)
    log = AccessLog(Recording(), capacity=100, batch=10_000, flush_s=1.0)
    started = time.perf_counter()
    for n in range(50_000):
        log.record("access.contracts.contract.read", envelope(n))
    elapsed = time.perf_counter() - started
    assert log.pending == 100
    assert log.dropped == 49_900
    assert elapsed < 1.0
    # Unusual input is still accepted without raising (the flusher deals with it later).
    log.record("", {})
    log.record("access.contracts.contract.read", {"event_id": object()})


async def test_the_flusher_publishes_in_batches_with_the_event_id_as_msg_id() -> None:
    publisher = Recording()
    log = AccessLog(publisher, capacity=1000, batch=100, flush_s=0.05)
    publisher.log = log
    for n in range(250):
        log.record("access.contracts.contract.read", envelope(n))
    async with running(log):
        await wait_until(lambda: len(publisher.calls) == 250)
    groups = [
        (pending, len(list(items)))
        for pending, items in itertools.groupby(p for _, _, p in publisher.calls)
    ]
    assert groups == [(150, 100), (50, 100), (0, 50)]
    assert [msg_id for _, msg_id, _ in publisher.calls] == [f"evt-{n}" for n in range(250)]
    assert {subject for subject, _, _ in publisher.calls} == {"access.contracts.contract.read"}


async def test_a_single_event_is_published_within_a_few_flush_intervals() -> None:
    publisher = Recording()
    log = AccessLog(publisher, batch=100, flush_s=0.05)
    async with running(log):
        await asyncio.sleep(0.01)
        log.record("access.contracts.contract.read", envelope(1))
        started = time.monotonic()
        await wait_until(lambda: len(publisher.calls) == 1, timeout_s=0.5)
        assert time.monotonic() - started < 0.2


async def test_a_failed_batch_is_dropped_counted_and_the_loop_recovers() -> None:
    publisher = Recording()
    publisher.fail_next = 1
    log = AccessLog(publisher, batch=10, flush_s=0.05, max_backoff_s=0.2)
    with structlog.testing.capture_logs() as captured:
        async with running(log):
            for n in range(3):
                log.record("access.contracts.contract.read", envelope(n))
            await wait_until(lambda: log.dropped == 3)
            assert log.dropped_by_reason == {"publish_failed": 3}
            assert log.pending == 0  # never re-queued
            log.record("access.contracts.contract.read", envelope(99))
            await wait_until(lambda: [m for _, m, _ in publisher.calls] == ["evt-99"])
    failed = [entry for entry in captured if entry["event"] == "access_events_publish_failed"]
    assert failed
    assert failed[0]["error"] == "ConnectionError"
    dropped = [entry for entry in captured if entry["event"] == "access_events_dropped"]
    assert [(d["dropped"], d["reason"]) for d in dropped] == [(3, "publish_failed")]


async def test_the_loop_backs_off_while_the_publisher_keeps_failing() -> None:
    publisher = Recording()
    publisher.fail_next = 10_000
    log = AccessLog(publisher, batch=1, flush_s=0.02, max_backoff_s=0.16)
    async with running(log):
        for _ in range(40):
            for n in range(5):
                log.record("access.contracts.contract.read", envelope(n))
            await asyncio.sleep(0.02)
    # Without a backoff the loop would try every 20 ms (about 40 attempts); doubling to 160 ms
    # leaves far fewer, and every attempt dropped what it took.
    assert 2 <= publisher.attempts <= 15
    assert log.dropped_by_reason["publish_failed"] == publisher.attempts


async def test_a_hanging_publisher_never_blocks_record() -> None:
    publisher = Hanging()
    log = AccessLog(publisher, capacity=3, batch=100, flush_s=0.02)
    async with running(log):
        log.record("access.contracts.contract.read", envelope(0))
        await wait_until(lambda: publisher.started == 1)
        started = time.perf_counter()
        for n in range(1, 11):
            log.record("access.contracts.contract.read", envelope(n))
        assert time.perf_counter() - started < 0.05
        assert log.pending == 3
        assert log.dropped_by_reason["queue_full"] == 7


async def test_a_publish_that_exceeds_its_timeout_counts_as_publish_failed() -> None:
    publisher = Hanging()
    log = AccessLog(publisher, batch=100, flush_s=0.02, publish_timeout_s=0.05)
    async with running(log):
        for n in range(4):
            log.record("access.contracts.contract.read", envelope(n))
        await wait_until(lambda: log.dropped == 4)
    assert log.dropped_by_reason == {"publish_failed": 4}


async def test_flush_returns_within_its_timeout_with_a_hanging_publisher() -> None:
    log = AccessLog(Hanging(), batch=2, flush_s=1.0)
    for n in range(5):
        log.record("access.contracts.contract.read", envelope(n))
    started = time.monotonic()
    await log.flush(0.1)
    assert time.monotonic() - started < 0.5
    assert log.pending == 0
    assert log.dropped == 5
    assert log.dropped_by_reason == {"shutdown": 5}


async def test_flush_publishes_everything_queued_when_the_publisher_works() -> None:
    publisher = Recording()
    log = AccessLog(publisher, batch=2, flush_s=1.0)
    for n in range(5):
        log.record("access.contracts.contract.read", envelope(n))
    await log.flush(1.0)
    assert [m for _, m, _ in publisher.calls] == [f"evt-{n}" for n in range(5)]
    assert log.dropped == 0


@pytest.mark.parametrize(
    "kwargs",
    [{"capacity": 0}, {"batch": 0}, {"flush_s": 0.0}, {"publish_timeout_s": -1.0}],
)
def test_a_degenerate_configuration_is_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="capacity and batch"):
        AccessLog(Recording(), **kwargs)


class _BrokenWake:
    def set(self) -> None:
        raise RuntimeError("event loop gone")


def test_record_swallows_an_internal_failure() -> None:
    log = AccessLog(Recording(), batch=1, flush_s=1.0)
    log._wake = _BrokenWake()  # type: ignore[assignment]
    log.record("access.contracts.contract.read", envelope(1))  # must not raise
    assert log.pending == 1


async def test_an_envelope_that_cannot_be_encoded_is_dropped_as_invalid() -> None:
    publisher = Recording()
    log = AccessLog(publisher, batch=10, flush_s=1.0)
    log.record("access.contracts.contract.read", {"n": 1})  # no event_id
    log.record("access.contracts.contract.read", {"event_id": "evt-x", "bad": object()})
    log.record("access.contracts.contract.read", envelope(2))
    await log.flush(1.0)
    assert [m for _, m, _ in publisher.calls] == ["evt-2"]
    assert log.dropped_by_reason == {"invalid": 2}


async def test_an_unexpected_loop_error_is_logged_and_the_loop_keeps_running() -> None:
    publisher = Recording()
    log = AccessLog(publisher, batch=1, flush_s=0.02)
    original = log._publish_batch
    calls = {"n": 0}

    async def flaky() -> Exception | None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("bug")
        return await original()

    log._publish_batch = flaky  # type: ignore[method-assign]
    with structlog.testing.capture_logs() as captured:
        async with running(log):
            log.record("access.contracts.contract.read", envelope(1))
            await wait_until(lambda: [m for _, m, _ in publisher.calls] == ["evt-1"])
    errors = [entry for entry in captured if entry["event"] == "access_log_loop_error"]
    assert [e["error"] for e in errors] == ["RuntimeError"]


async def test_flush_stops_at_a_failing_publisher_and_drops_the_rest_as_shutdown() -> None:
    publisher = Recording()
    publisher.fail_next = 1
    log = AccessLog(publisher, batch=2, flush_s=1.0)
    for n in range(5):
        log.record("access.contracts.contract.read", envelope(n))
    await log.flush(1.0)
    assert publisher.attempts == 1
    assert log.pending == 0
    assert log.dropped_by_reason == {"publish_failed": 2, "shutdown": 3}


async def test_the_hook_without_nats_leaves_no_access_log() -> None:
    app = FastAPI()
    async with access_log_hook(CoreSettings(service="probe"))(app):
        assert app.state.access_log is None
    assert app.state.access_log is None


async def test_the_hook_runs_an_access_log_and_never_connects_when_nothing_was_recorded() -> None:
    # Nothing listens on this port: the publisher connects lazily, so an idle app shuts down
    # cleanly without ever reaching the broker.
    settings = CoreSettings(service="probe", nats_url="nats://localhost:1")
    app = FastAPI()
    async with access_log_hook(settings, flush_timeout_s=0.5)(app):
        access_log = app.state.access_log
        assert isinstance(access_log, AccessLog)
        assert access_log.pending == 0
    assert app.state.access_log is None
    assert access_log.dropped == 0
