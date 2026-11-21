"""The audit path survives NATS outages, a missing stream, poison messages and late commits."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest
from dc_audit.settings import AuditSettings
from dc_core.events import PublishAck, encode_envelope, subject_for

pytestmark = pytest.mark.integration


class RecordingPublisher:
    """In-memory EventPublisher: no NATS involved (D-21 seam)."""

    def __init__(self, fail_on: int | None = None) -> None:
        self.sent: list[tuple[str, bytes, str]] = []
        self.fail_on = fail_on
        self.closed = False

    async def publish(self, subject: str, payload: bytes, *, msg_id: str) -> PublishAck:
        if self.fail_on is not None and len(self.sent) == self.fail_on:
            raise ConnectionError("broker unavailable")
        self.sent.append((subject, payload, msg_id))
        return PublishAck(duplicate=False)

    async def close(self) -> None:
        self.closed = True


async def _outbox(env: Any) -> list[dict[str, Any]]:
    return await env.fetch("SELECT * FROM outbox ORDER BY id")


async def _stored_event_ids(env: Any) -> list[uuid.UUID]:
    rows = await env.fetch("SELECT event_id FROM audit_event ORDER BY id")
    return [r["event_id"] for r in rows]


async def test_relay_publisher_seam_publishes_in_id_order_with_event_id_as_msg_id(
    audit_env: Any,
) -> None:
    ids = [(await audit_env.write_event("widget.created"))[0] for _ in range(3)]
    publisher = RecordingPublisher()
    relay = audit_env.relay(publisher)
    assert await relay.drain_once() == 3
    assert [msg_id for _, _, msg_id in publisher.sent] == [str(i) for i in ids]
    assert {subject for subject, _, _ in publisher.sent} == {"audit.probe.widget.widget.created"}
    assert all(row["published_at"] is not None for row in await _outbox(audit_env))
    assert await relay.drain_once() == 0  # nothing left to publish


async def test_relay_publisher_seam_failure_leaves_the_row_unpublished_with_attempts(
    audit_env: Any,
) -> None:
    first, _ = await audit_env.write_event()
    second, _ = await audit_env.write_event()
    publisher = RecordingPublisher(fail_on=1)  # the second publish fails
    relay = audit_env.relay(publisher)
    with pytest.raises(ConnectionError):
        await relay.drain_once()
    rows = {row["event_id"]: row for row in await _outbox(audit_env)}
    assert rows[first]["published_at"] is not None  # successes before the failure are kept
    assert rows[first]["attempts"] == 0
    assert rows[second]["published_at"] is None
    assert rows[second]["attempts"] == 1

    publisher.fail_on = None
    assert await relay.drain_once() == 1
    assert all(row["published_at"] is not None for row in await _outbox(audit_env))


async def test_relay_publisher_seam_rejects_an_unpublishable_subject_without_publishing(
    audit_env: Any,
) -> None:
    await audit_env.write_event("bad action")  # the producer wrote an action NATS cannot carry
    publisher = RecordingPublisher()
    with pytest.raises(ValueError, match="action"):
        await audit_env.relay(publisher).drain_once()
    assert publisher.sent == []
    (row,) = await _outbox(audit_env)
    assert row["published_at"] is None
    assert row["attempts"] == 1


async def test_relay_run_stops_cleanly_when_cancelled_and_publishes_in_the_background(
    audit_env: Any, eventually: Any
) -> None:
    import asyncio

    event_id, _ = await audit_env.write_event()
    publisher = RecordingPublisher()
    task = asyncio.create_task(audit_env.relay(publisher, poll_s=0.05).run())

    async def _published() -> bool:
        return bool(publisher.sent)

    try:
        await eventually(_published)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert [m for _, _, m in publisher.sent] == [str(event_id)]


async def test_only_one_relay_replica_holds_the_advisory_lock(
    audit_env: Any, eventually: Any
) -> None:
    import asyncio

    first, second = RecordingPublisher(), RecordingPublisher()
    t1 = asyncio.create_task(audit_env.relay(first, poll_s=0.05).run())

    async def _locked() -> bool:
        rows = await audit_env.fetch(
            "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        )
        return rows[0]["n"] == 1

    await eventually(_locked)
    t2 = asyncio.create_task(audit_env.relay(second, poll_s=0.05).run())
    try:
        await audit_env.write_event()

        async def _sent() -> bool:
            return bool(first.sent)

        await eventually(_sent)
        await asyncio.sleep(0.3)  # the second replica had time to (wrongly) pick the row up
        assert second.sent == []
    finally:
        for task in (t1, t2):
            task.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)


async def test_relay_missing_stream_fails_without_marking_published_then_recovers(
    audit_env: Any, run_consumer: Any, eventually: Any, stream_exists: Any
) -> None:
    event_id, _ = await audit_env.write_event()
    publisher = audit_env.publisher(timeout_s=2.0)
    relay = audit_env.relay(publisher)
    try:
        with pytest.raises(Exception):  # noqa: B017, PT011 - NoStreamResponse / NoResponders
            await relay.drain_once()
        (row,) = await _outbox(audit_env)
        assert row["published_at"] is None
        assert row["attempts"] == 1

        run_consumer(audit_env.consumer())  # ensure_stream creates AUDIT
        await eventually(stream_exists)
        assert await relay.drain_once() == 1

        async def _stored() -> bool:
            return await _stored_event_ids(audit_env) == [event_id]

        await eventually(_stored)
    finally:
        await publisher.close()


async def test_relay_outage_keeps_rows_unpublished_then_delivers_exactly_once(
    audit_env: Any, nats_outage: Any, run_consumer: Any, eventually: Any
) -> None:
    env = dataclasses.replace(
        audit_env,
        nats_url=nats_outage.url,
        settings=AuditSettings(database_url=audit_env.db_url, nats_url=nats_outage.url),  # type: ignore[arg-type]
    )
    run_consumer(env.consumer())

    async def _stream_ready() -> bool:
        import nats

        nc = await nats.connect(servers=[env.nats_url])
        try:
            await nc.jetstream().stream_info("AUDIT")
            return True
        except Exception:
            return False
        finally:
            await nc.close()

    await eventually(_stream_ready)
    publisher = env.publisher(timeout_s=1.0)
    relay = env.relay(publisher)
    try:
        first, _ = await env.write_event()
        assert await relay.drain_once() == 1  # healthy path first, so the connection is live

        second, _ = await env.write_event()
        nats_outage.pause()
        try:
            with pytest.raises(Exception):  # noqa: B017, PT011 - timeout while NATS is frozen
                await relay.drain_once()
            row = next(r for r in await _outbox(env) if r["event_id"] == second)
            assert row["published_at"] is None
            assert row["attempts"] >= 1
        finally:
            nats_outage.unpause()

        async def _drained() -> bool:
            try:
                return await relay.drain_once() >= 1
            except Exception:
                return False

        await eventually(_drained, within_s=30)

        async def _both_stored() -> bool:
            return sorted(await _stored_event_ids(env)) == sorted([first, second])

        await eventually(_both_stored, within_s=30)
        assert len(await _stored_event_ids(env)) == 2  # exactly once each
    finally:
        await publisher.close()


async def test_relay_ordering_late_commit_of_a_lower_id_is_still_published(
    audit_env: Any, run_consumer: Any, eventually: Any, stream_exists: Any
) -> None:
    run_consumer(audit_env.consumer())
    await eventually(stream_exists)
    publisher = audit_env.publisher()
    relay = audit_env.relay(publisher)
    try:
        # Row A takes the lower id but stays uncommitted while row B commits and is published.
        event_a = uuid.uuid4()
        async with audit_env.session_factory() as open_session:
            open_session.add(
                audit_env.outbox_model(
                    event_id=event_a,
                    aggregate_type="widget",
                    aggregate_id="late",
                    action="widget.created",
                    actor=audit_env.actor.model_dump(),
                    before=None,
                    after={"id": "late"},
                )
            )
            await open_session.flush()

            event_b, _ = await audit_env.write_event(aggregate_id="early")
            assert await relay.drain_once() == 1  # only B is visible
            assert [r["event_id"] for r in await _outbox(audit_env)] == [event_b]

            await open_session.commit()

        rows = {r["event_id"]: r["id"] for r in await _outbox(audit_env)}
        assert rows[event_a] < rows[event_b]  # A really has the lower id
        assert await relay.drain_once() == 1  # no high-water-mark: A is published after B

        async def _both() -> bool:
            return sorted(await _stored_event_ids(audit_env)) == sorted([event_a, event_b])

        await eventually(_both)
        assert len(await _stored_event_ids(audit_env)) == 2
    finally:
        await publisher.close()


async def test_poison_message_is_dead_lettered_after_max_deliver_and_does_not_block(
    audit_env: Any, run_consumer: Any, eventually: Any, stream_exists: Any
) -> None:
    run_consumer(audit_env.consumer(max_nak_delay_s=0.05))
    await eventually(stream_exists)
    publisher = audit_env.publisher()
    try:
        await publisher.publish("audit.probe.x", b"this is not json", msg_id="poison-1")

        async def _dead() -> list[dict[str, Any]]:
            return await audit_env.fetch("SELECT * FROM audit_dead_letter")

        (dead,) = await eventually(_dead, within_s=30)
        assert dead["subject"] == "audit.probe.x"
        assert dead["payload"] == "this is not json"
        assert dead["deliveries"] == 10
        assert dead["created_by"] == "system:audit-ingest"
        assert dead["error"].startswith("ValidationError")

        # The poison message was acknowledged: a good message published afterwards is ingested.
        event_id, _ = await audit_env.write_event()
        relay = audit_env.relay(publisher)
        assert await relay.drain_once() == 1

        async def _stored() -> bool:
            return await _stored_event_ids(audit_env) == [event_id]

        await eventually(_stored)
        assert len(await audit_env.fetch("SELECT 1 FROM audit_dead_letter")) == 1
    finally:
        await publisher.close()


async def test_non_utf8_poison_payload_is_kept_as_base64(
    audit_env: Any, run_consumer: Any, eventually: Any, stream_exists: Any
) -> None:
    run_consumer(audit_env.consumer(max_nak_delay_s=0.05))
    await eventually(stream_exists)
    publisher = audit_env.publisher()
    try:
        await publisher.publish("audit.probe.y", b"\xff\xfe\x00", msg_id="poison-2")

        async def _dead() -> list[dict[str, Any]]:
            return await audit_env.fetch("SELECT * FROM audit_dead_letter")

        (dead,) = await eventually(_dead, within_s=30)
        assert dead["payload"] == "base64://4A"
    finally:
        await publisher.close()


async def test_mismatched_subject_is_dead_lettered_not_ingested(
    audit_env: Any, run_consumer: Any, eventually: Any, stream_exists: Any
) -> None:
    run_consumer(audit_env.consumer(max_nak_delay_s=0.05))
    await eventually(stream_exists)
    event_id = uuid.uuid4()
    payload = encode_envelope(
        {
            "event_id": str(event_id),
            "occurred_at": "2026-11-09T12:00:00+00:00",
            "service": "identity",
            "aggregate_type": "user",
            "aggregate_id": "u",
            "aggregate_version": None,
            "scope_path": [],
            "action": "user.created",
            "actor": {"type": "user", "id": "fake:x", "display": "x", "via": "jwt"},
            "request_id": None,
            "before": None,
            "after": {"id": "u"},
            "schema_version": 1,
        }
    )
    publisher = audit_env.publisher()
    try:
        # Published on probe's subtree but claiming to be identity.
        await publisher.publish(
            subject_for("probe", "user", "user.created"), payload, msg_id=str(event_id)
        )

        async def _dead() -> list[dict[str, Any]]:
            return await audit_env.fetch("SELECT * FROM audit_dead_letter")

        (dead,) = await eventually(_dead, within_s=30)
        assert dead["error"].startswith("SubjectMismatchError")
        assert await _stored_event_ids(audit_env) == []
    finally:
        await publisher.close()
