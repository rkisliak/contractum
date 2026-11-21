"""Tracer: a committed outbox event reaches the audit store through NATS JetStream."""

from __future__ import annotations

import jsonpatch
import nats
import pytest

pytestmark = pytest.mark.integration


async def test_committed_outbox_event_is_stored_once_in_audit_through_jetstream(
    audit_env, run_consumer, eventually, stream_exists
) -> None:  # type: ignore[no-untyped-def]
    # Raw core-NATS listener: JetStream publishes are also delivered to plain subscribers, which
    # is how the test reads the subject and the Nats-Msg-Id header.
    listener = await nats.connect(servers=[audit_env.nats_url])
    seen: list[tuple[str, str | None]] = []

    async def _on_message(msg) -> None:  # type: ignore[no-untyped-def]
        seen.append((msg.subject, (msg.headers or {}).get("Nats-Msg-Id")))

    await listener.subscribe("audit.>", cb=_on_message)
    await listener.flush()

    run_consumer(audit_env.consumer())  # creates stream AUDIT on start
    await eventually(stream_exists)

    event_id, aggregate_id = await audit_env.write_event(
        "widget.created", after={"id": "w-1", "name": "first"}
    )
    publisher = audit_env.publisher()
    relay = audit_env.relay(publisher)
    try:
        assert await relay.drain_once() == 1

        async def _stored() -> list[dict]:  # type: ignore[type-arg]
            return await audit_env.fetch(
                "SELECT * FROM audit_event WHERE event_id = :e", e=event_id
            )

        (row,) = await eventually(_stored)
    finally:
        await publisher.close()
        await listener.drain()

    assert row["service"] == "probe"
    assert row["aggregate_type"] == "widget"
    assert row["aggregate_id"] == aggregate_id
    assert row["action"] == "widget.created"
    assert row["actor"] == {
        "type": "user",
        "id": "fake:admin",
        "display": "Admin Display",
        "via": "jwt",
        "credential_id": "j1",
        "on_behalf_of": None,
    }
    assert row["before"] is None  # SQL NULL, not the JSON value null
    assert row["after"] == {"id": "w-1", "name": "first"}
    assert row["changes"] == jsonpatch.make_patch({}, {"id": "w-1", "name": "first"}).patch
    assert row["created_by"] == "system:audit-ingest"
    assert row["updated_by"] == "system:audit-ingest"
    assert row["schema_version"] == 1

    # The dotted action "widget.created" became two subject tokens, and the header is the event id.
    assert ("audit.probe.widget.widget.created", str(event_id)) in seen

    (outbox,) = await audit_env.fetch(
        "SELECT published_at FROM outbox WHERE event_id = :e", e=event_id
    )
    assert outbox["published_at"] is not None
    assert (await audit_env.fetch("SELECT count(*) AS n FROM audit_event"))[0]["n"] == 1


async def test_audit_migration_matches_the_models(audit_env) -> None:  # type: ignore[no-untyped-def]
    from dc_audit.migrate import SCRIPT_LOCATION, VERSION_TABLE
    from dc_core.migrations import run_alembic

    # `alembic check` raises when autogenerate would produce a diff; the probe outbox table that
    # the fixture created lives outside the audit metadata, so only drop it from the comparison.
    await audit_env.fetch("SELECT 1")
    async with audit_env.engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE outbox")
    run_alembic(
        script_location=SCRIPT_LOCATION,
        version_table=VERSION_TABLE,
        url=audit_env.db_url,
        command="check",
    )
    rows = await audit_env.fetch("SELECT version_num FROM alembic_version_audit")
    assert [r["version_num"] for r in rows] == ["0002_audit_query"]
