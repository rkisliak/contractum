"""The usage recorder keeps last-used timestamps in memory and never touches the database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from dc_identity.credentials import CredentialUsageRecorder

pytestmark = pytest.mark.unit


def _no_database() -> Any:
    raise AssertionError("record() must never open a database session")


def test_record_is_memory_only_and_keeps_the_latest_timestamp_per_credential() -> None:
    recorder = CredentialUsageRecorder(_no_database)
    first, second = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    recorder.record(first, now)
    recorder.record(first, now - timedelta(minutes=1))
    recorder.record(second, now)
    assert recorder.pending_count == 2
    assert recorder.pending[first] == now


def test_new_credentials_are_dropped_once_max_pending_is_reached() -> None:
    recorder = CredentialUsageRecorder(_no_database, max_pending=2)
    now = datetime.now(UTC)
    ids = [uuid.uuid4() for _ in range(3)]
    for credential_id in ids:
        recorder.record(credential_id, now)
    assert recorder.pending_count == 2
    assert ids[2] not in recorder.pending
    # A credential already pending is still refreshed.
    recorder.record(ids[0], now + timedelta(seconds=5))
    assert recorder.pending[ids[0]] == now + timedelta(seconds=5)


def test_a_full_recorder_warns_once_until_the_next_flush() -> None:
    recorder = CredentialUsageRecorder(_no_database, max_pending=1)
    now = datetime.now(UTC)
    recorder.record(uuid.uuid4(), now)
    with structlog.testing.capture_logs() as logs:
        recorder.record(uuid.uuid4(), now)
        recorder.record(uuid.uuid4(), now)
    assert [entry["event"] for entry in logs] == ["credential_usage_pending_full"]
    assert recorder.pending_count == 1


class _BrokenSession:
    async def __aenter__(self) -> Any:
        raise ConnectionError("database is down")

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_a_failed_flush_drops_the_batch_and_logs_only_the_error_class() -> None:
    recorder = CredentialUsageRecorder(_BrokenSession)
    credential_id = uuid.uuid4()
    recorder.record(credential_id, datetime.now(UTC))
    with structlog.testing.capture_logs() as logs:
        flushed = await recorder.flush()
    assert flushed == 0
    assert recorder.pending_count == 0
    assert logs == [
        {
            "event": "credential_usage_flush_failed",
            "error": "ConnectionError",
            "log_level": "error",
        }
    ]
    assert str(credential_id) not in repr(logs)


async def test_an_empty_flush_opens_no_session() -> None:
    assert await CredentialUsageRecorder(_no_database).flush() == 0
