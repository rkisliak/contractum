"""The audit consumer's failure paths, with in-memory NATS and session stand-ins (T-01-20).

The happy paths and the dead letter run against real NATS in the integration suite; these cover
what a real broker cannot be made to do on demand: a stream setup that fails after connecting, a
close that raises, an empty fetch, a dead-letter write that fails, and the loop's backoff.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any, cast

import nats.errors
import pytest
from dc_audit import consumer as consumer_module
from dc_audit.consumer import AuditConsumer
from dc_audit.settings import AuditSettings
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit

NATS_URL = "nats://nats.invalid:4222"


class _Client:
    """A NATS client stand-in that records ``close`` and can fail it."""

    def __init__(self, *, closed: bool = False, close_error: Exception | None = None) -> None:
        self.is_closed = closed
        self.close_calls = 0
        self._close_error = close_error
        self.js = SimpleNamespace(stream_info=self._stream_info)

    async def _stream_info(self, name: str) -> Any:
        raise ConnectionResetError(f"stream_info {name}")

    def jetstream(self) -> Any:
        return self.js

    async def close(self) -> None:
        self.close_calls += 1
        if self._close_error is not None:
            raise self._close_error


class _Msg:
    """A JetStream message stand-in recording acks and naks."""

    def __init__(self, delivered: int) -> None:
        self.subject = "audit.probe.widget.created"
        self.data = b"{}"
        self.metadata = SimpleNamespace(num_delivered=delivered)
        self.acked = False
        self.nak_delays: list[float] = []

    async def ack(self) -> None:
        self.acked = True

    async def nak(self, delay: float) -> None:
        self.nak_delays.append(delay)


def _failing_session_factory() -> Any:
    raise ConnectionRefusedError("database down")


def _consumer(**settings: Any) -> AuditConsumer:
    return AuditConsumer(
        cast(Any, _failing_session_factory), AuditSettings(**{"nats_url": NATS_URL, **settings})
    )


async def test_a_consumer_without_a_nats_url_refuses_to_subscribe() -> None:
    with pytest.raises(RuntimeError, match=r"settings\.nats_url"):
        await _consumer(nats_url=None).consume_once()


async def test_a_failed_stream_setup_closes_the_new_connection_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    seen: dict[str, Any] = {}

    async def _connect(**kwargs: Any) -> _Client:
        seen.update(kwargs)
        return client

    monkeypatch.setattr(consumer_module.nats, "connect", _connect)
    consumer = _consumer(nats_user="audit", nats_password="unit-test-value")
    with pytest.raises(ConnectionResetError, match="stream_info AUDIT"):
        await consumer.consume_once()
    assert client.close_calls == 1
    assert seen["servers"] == [NATS_URL]
    assert (seen["user"], seen["password"]) == ("audit", "unit-test-value")
    # Nothing half-built is kept: the next pass connects again.
    assert consumer._nc is None
    assert consumer._sub is None


async def test_closing_without_a_connection_does_nothing() -> None:
    consumer = _consumer()
    with capture_logs() as logs:
        await consumer.close()
    assert logs == []


async def test_closing_an_already_closed_connection_does_not_close_it_again() -> None:
    consumer = _consumer()
    client = _Client(closed=True)
    consumer._nc = cast(Any, client)
    await consumer.close()
    assert client.close_calls == 0
    assert consumer._nc is None


async def test_a_close_that_fails_is_logged_and_forgets_the_connection() -> None:
    consumer = _consumer()
    client = _Client(close_error=OSError("broken pipe"))
    consumer._nc = cast(Any, client)
    consumer._sub = object()
    with capture_logs() as logs:
        await consumer.close()
    assert client.close_calls == 1
    assert (consumer._nc, consumer._sub) == (None, None)
    assert logs == [
        {"event": "audit_consumer_close_failed", "error": "OSError", "log_level": "warning"}
    ]


@pytest.mark.parametrize("empty", [nats.errors.TimeoutError, consumer_module.FetchTimeoutError])
async def test_an_empty_fetch_handles_nothing(empty: type[Exception]) -> None:
    calls: list[tuple[int, float]] = []

    async def _fetch(batch: int, **options: float) -> list[Any]:
        calls.append((batch, options["timeout"]))
        raise empty()

    consumer = _consumer(consumer_batch=7)
    consumer._sub = SimpleNamespace(fetch=_fetch)
    assert await consumer.consume_once(timeout_s=0.25) == 0
    assert calls == [(7, 0.25)]


async def test_a_failed_dead_letter_write_naks_instead_of_acking() -> None:
    consumer = _consumer(max_deliver=3)
    msg = _Msg(delivered=3)
    with capture_logs() as logs:
        await consumer._on_failure(cast(Any, msg), ValueError("bad envelope"))
    assert msg.acked is False
    assert msg.nak_delays == [8.0]  # 2**3, under the 30 s cap
    assert [entry["event"] for entry in logs] == ["audit_ingest_failed", "audit_dead_letter_failed"]
    assert logs[1]["error"] == "ConnectionRefusedError"


def _scripted(consumer: AuditConsumer, outcomes: Iterable[Any]) -> list[str]:
    """Replace ``consume_once`` with a script (exceptions are raised) and count ``close``."""
    script = iter(outcomes)
    events: list[str] = []

    async def _consume_once(timeout_s: float = 5.0) -> int:
        outcome = next(script)
        if isinstance(outcome, BaseException):
            raise outcome
        return int(outcome)

    async def _close() -> None:
        events.append("close")

    consumer.consume_once = _consume_once
    consumer.close = _close
    return events


async def test_the_loop_backs_off_on_errors_resets_after_a_success_and_closes_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer = _consumer()
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(
        consumer_module,
        "asyncio",
        SimpleNamespace(sleep=_sleep, CancelledError=asyncio.CancelledError),
    )
    error = ConnectionError("nats gone")
    closes = _scripted(consumer, [error, 2, error, error, asyncio.CancelledError()])
    with capture_logs() as logs, pytest.raises(asyncio.CancelledError):
        await consumer.run()
    assert sleeps == [0.5, 0.5, 1.0]
    assert len(closes) == 4  # once per error (reconnect) and once on the way out
    assert [entry["event"] for entry in logs] == ["audit_consumer_loop_error"] * 3
    assert {entry["error"] for entry in logs} == {"ConnectionError"}
