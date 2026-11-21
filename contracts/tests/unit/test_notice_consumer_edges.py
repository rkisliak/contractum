"""``NoticeConsumer`` without a broker: configuration, a broken close and a dropped bad payload."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import nats.errors
import pytest
from dc_contracts import notice_consumer
from dc_contracts.notice_consumer import NoticeConsumer
from dc_contracts.settings import ContractsSettings
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit

DB = "postgresql+psycopg://nobody:nobody@127.0.0.1:1/never"
SUBJECT = "audit.contracts.contract.version_published"


def _settings(**overrides: Any) -> ContractsSettings:
    return ContractsSettings(database_url=DB, **overrides)  # type: ignore[arg-type]


class _Msg:
    def __init__(self, data: bytes, delivered: int) -> None:
        self.data = data
        self.subject = SUBJECT
        self.metadata = SimpleNamespace(num_delivered=delivered)
        self.acked = 0
        self.naked: list[float] = []

    async def ack(self) -> None:
        self.acked += 1

    async def nak(self, delay: float) -> None:
        self.naked.append(delay)


class _Sub:
    def __init__(self, batches: list[list[_Msg]]) -> None:
        self._batches = batches

    async def fetch(self, batch: int, **_kwargs: Any) -> list[_Msg]:
        if not self._batches:
            raise nats.errors.TimeoutError
        return self._batches.pop(0)


class _Nats:
    """A connected client whose close fails, as a broken socket can."""

    def __init__(self, sub: _Sub) -> None:
        self.is_closed = False
        self._sub = sub
        self.close_calls = 0

    def jetstream(self) -> Any:
        async def pull_subscribe(*_args: Any, **_kwargs: Any) -> _Sub:
            return self._sub

        return SimpleNamespace(pull_subscribe=pull_subscribe)

    async def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError("socket already gone")


def _connected(monkeypatch: pytest.MonkeyPatch, batches: list[list[_Msg]]) -> _Nats:
    client = _Nats(_Sub(batches))

    async def connect(**_kwargs: Any) -> _Nats:
        return client

    monkeypatch.setattr(notice_consumer.nats, "connect", connect)
    return client


async def test_a_consumer_without_a_nats_url_refuses_to_start() -> None:
    consumer = NoticeConsumer(None, _settings(nats_url=None))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="nats_url"):
        await consumer.consume_once(timeout_s=0.01)


async def test_a_close_that_fails_is_logged_by_class_and_never_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _connected(monkeypatch, [])
    consumer = NoticeConsumer(None, _settings(nats_url="nats://nats:4222"))  # type: ignore[arg-type]
    assert await consumer.consume_once(timeout_s=0.01) == 0
    with capture_logs() as logs:
        await consumer.close()
    assert client.close_calls == 1
    assert [(e["event"], e["error"]) for e in logs] == [
        ("notice_consumer_close_failed", "RuntimeError")
    ]
    await consumer.close()  # the client is already released: nothing to close again
    assert client.close_calls == 1


@pytest.mark.parametrize("data", [b"{not json", b"[1, 2]", b'{"event_id": 7}'])
async def test_a_payload_without_a_readable_event_id_is_dropped_with_no_id(
    monkeypatch: pytest.MonkeyPatch, data: bytes
) -> None:
    async def failing(*_args: Any, **_kwargs: Any) -> int:
        raise ConnectionError("database down")

    monkeypatch.setattr(notice_consumer, "handle_payload", failing)
    settings = _settings(nats_url="nats://nats:4222")
    msg = _Msg(data, delivered=settings.notice_consumer_max_deliver)
    _connected(monkeypatch, [[msg]])
    consumer = NoticeConsumer(None, settings)  # type: ignore[arg-type]
    with capture_logs() as logs:
        assert await consumer.consume_once(timeout_s=0.01) == 1
    (dropped,) = [e for e in logs if e["event"] == "notice_event_dropped"]
    assert dropped["event_id"] is None
    assert dropped["error"] == "ConnectionError"
    assert (msg.acked, msg.naked) == (1, [])
