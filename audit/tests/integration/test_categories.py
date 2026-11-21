"""Audit categories (D-170): change, access and denial rows, derived at ingest, never trusted."""

from __future__ import annotations

import uuid
from typing import Any

import nats
import pytest
import respx
from dc_audit.ingest import SubjectMismatchError, handle_payload
from dc_auth.issuers import AuthSettings
from dc_auth.testing import FakeIssuer
from dc_core.events import ACCESS_STREAM, AUDIT_STREAM, encode_envelope, subject_for
from nats.js.api import DiscardPolicy, StorageType, StreamConfig
from pydantic import ValidationError
from sqlalchemy import text

pytestmark = pytest.mark.integration

ROOT, ORG, TEAM = (uuid.uuid4() for _ in range(3))
DENIAL_SUBJECT = "audit.identity.authz.check.denied"
ACCESS_SUBJECT = "access.contracts.contract.read"
PDP = "http://identity:8000/internal/v1/authz/check"
EVENTS = "/internal/v1/events"


def _change(**overrides: Any) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "occurred_at": "2026-11-14T12:00:00+00:00",
        "service": "probe",
        "aggregate_type": "widget",
        "aggregate_id": "w-1",
        "aggregate_version": 1,
        "scope_path": [str(ROOT), str(ORG), str(TEAM)],
        "action": "widget.created",
        "actor": {"type": "user", "id": "fake:admin", "display": "Admin", "via": "jwt"},
        "request_id": "req-1",
        "before": None,
        "after": {"id": "w-1"},
        "schema_version": 1,
    }
    envelope.update(overrides)
    return envelope


def _denial(**overrides: Any) -> dict[str, Any]:
    envelope = _change(
        service="identity",
        aggregate_type="authz",
        aggregate_id=str(TEAM),
        action="check.denied",
        after=None,
        detail={
            "permission": "contract:publish",
            "outcome": "forbidden",
            "service": "contracts",
            "route": "/api/v1/contracts/contracts/{contract_id}/drafts/{draft_id}/publish",
        },
    )
    envelope.update(overrides)
    return envelope


def _access(**overrides: Any) -> dict[str, Any]:
    contract_id = str(uuid.uuid4())
    envelope = _change(
        service="contracts",
        aggregate_type="contract",
        aggregate_id=contract_id,
        aggregate_version=None,
        action="read",
        after=None,
        category="access",
        detail={
            "contract_id": contract_id,
            "version_id": str(uuid.uuid4()),
            "semver": "1.2.0",
            "ref": "latest",
            "media_type": "application/json",
        },
    )
    envelope.update(overrides)
    return envelope


async def _rows(audit_env: Any) -> list[dict[str, Any]]:
    return await audit_env.fetch(
        "SELECT event_id, category, detail, leaf_folder_id, scope_path FROM audit_event ORDER BY id"
    )


async def test_a_denial_is_stored_once_as_category_denial_with_its_detail(audit_env: Any) -> None:
    envelope = _denial()
    payload = encode_envelope(envelope)
    assert subject_for("identity", "authz", "check.denied") == DENIAL_SUBJECT

    assert await handle_payload(audit_env.session_factory, payload, subject=DENIAL_SUBJECT) is True
    assert await handle_payload(audit_env.session_factory, payload, subject=DENIAL_SUBJECT) is False

    (row,) = await _rows(audit_env)
    assert str(row["event_id"]) == envelope["event_id"]
    assert row["category"] == "denial"
    assert row["detail"] == envelope["detail"]


async def test_an_envelope_without_category_or_detail_is_a_change(audit_env: Any) -> None:
    envelope = _change()
    subject = subject_for("probe", "widget", "widget.created")
    assert await handle_payload(
        audit_env.session_factory, encode_envelope(envelope), subject=subject
    )
    (row,) = await _rows(audit_env)
    assert row["category"] == "change"
    assert row["detail"] is None  # SQL NULL


async def test_leaf_folder_id_is_the_last_scope_path_element_or_null(audit_env: Any) -> None:
    deep = _change()
    empty = _change(scope_path=[])
    for envelope in (deep, empty):
        assert await handle_payload(audit_env.session_factory, encode_envelope(envelope))
    first, second = await _rows(audit_env)
    assert first["leaf_folder_id"] == TEAM
    assert second["leaf_folder_id"] is None


async def test_rows_written_before_the_migration_read_category_change(audit_env: Any) -> None:
    from dc_audit.migrate import SCRIPT_LOCATION, VERSION_TABLE
    from dc_core.migrations import run_alembic

    def _alembic(command: str, revision: str) -> None:
        run_alembic(
            script_location=SCRIPT_LOCATION,
            version_table=VERSION_TABLE,
            url=audit_env.db_url,
            command=command,
            revision=revision,
        )

    _alembic("downgrade", "0001_audit_baseline")
    async with audit_env.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO audit_event (event_id, service, aggregate_type, aggregate_id, action,"
                " scope_path, actor, occurred_at, changes, schema_version, created_by, updated_by)"
                " VALUES (gen_random_uuid(), 'probe', 'widget', 'w-old', 'widget.created',"
                ' ARRAY[CAST(:root AS uuid), CAST(:org AS uuid)], \'{"id": "fake:admin"}\','
                " now(), '[]', 1, 'system:audit-ingest', 'system:audit-ingest')"
            ),
            {"root": str(ROOT), "org": str(ORG)},
        )
    _alembic("upgrade", "head")
    (row,) = await _rows(audit_env)
    assert row["category"] == "change"
    assert row["detail"] is None
    assert row["leaf_folder_id"] == ORG


async def test_leaf_folder_id_is_stored_and_the_query_indexes_exist(audit_env: Any) -> None:
    (column,) = await audit_env.fetch(
        "SELECT attgenerated FROM pg_attribute"
        " WHERE attrelid = 'audit_event'::regclass AND attname = 'leaf_folder_id'"
    )
    assert column["attgenerated"] == "s"  # STORED, not PostgreSQL 18's virtual default
    names = {
        row["indexname"]
        for row in await audit_env.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'audit_event'"
        )
    }
    assert {
        "ix_audit_event_time",
        "ix_audit_event_leaf_time",
        "ix_audit_event_aggregate_time",
        "ix_audit_event_actor_time",
        "ix_audit_event_service_action",
    } <= names
    assert not {"ix_audit_event_occurred_at", "ix_audit_event_aggregate_id"} & names
    (check,) = await audit_env.fetch(
        "SELECT conname FROM pg_constraint"
        " WHERE conrelid = 'audit_event'::regclass AND contype = 'c'"
    )
    assert check["conname"] == "ck_audit_event_category"


# --- Task 2: the ACCESS stream, spoof refusals, detail caps and the change-only internal read ---


async def _stream_config(nats_url: str, name: str) -> StreamConfig | None:
    nc = await nats.connect(servers=[nats_url])
    try:
        info = await nc.jetstream().stream_info(name)
    except Exception:
        return None
    finally:
        await nc.close()
    return info.config


async def test_consumer_hook_creates_access_and_leaves_an_existing_audit_stream_alone(
    audit_env: Any, eventually: Any
) -> None:
    from dc_audit.consumer import consumer_hook
    from fastapi import FastAPI

    nc = await nats.connect(servers=[audit_env.nats_url])
    try:
        # A pre-existing AUDIT stream with a non-default window: the hook must not edit it.
        await nc.jetstream().add_stream(
            StreamConfig(
                name=AUDIT_STREAM,
                subjects=["audit.>"],
                storage=StorageType.FILE,
                duplicate_window=60,
            )
        )
    finally:
        await nc.close()

    app = FastAPI()
    app.state.session_factory = audit_env.session_factory
    async with consumer_hook(audit_env.settings)(app):

        async def _access() -> StreamConfig | None:
            return await _stream_config(audit_env.nats_url, ACCESS_STREAM)

        access = await eventually(_access, within_s=15)

    assert access.subjects == ["access.>"]
    assert access.storage == StorageType.FILE
    assert access.max_age == 7 * 24 * 3600
    # WR-03: bounded by size too, so it can never fill the store the AUDIT stream shares.
    assert access.max_bytes == audit_env.settings.access_max_bytes == 1024**3
    assert access.discard == DiscardPolicy.OLD
    assert access.duplicate_window == 120
    audit = await _stream_config(audit_env.nats_url, AUDIT_STREAM)
    assert audit is not None
    assert audit.duplicate_window == 60
    assert audit.max_bytes in (None, -1)
    assert audit.max_age in (None, 0)


async def test_an_existing_access_stream_without_a_size_cap_gets_one_and_nothing_else_changes(
    audit_env: Any, eventually: Any
) -> None:
    """WR-03: ACCESS created before the size cap existed is updated in place, not recreated."""
    from dc_audit.consumer import consumer_hook
    from fastapi import FastAPI

    nc = await nats.connect(servers=[audit_env.nats_url])
    try:
        await nc.jetstream().add_stream(
            StreamConfig(
                name=ACCESS_STREAM,
                subjects=["access.>"],
                storage=StorageType.FILE,
                duplicate_window=60,
                max_age=7 * 24 * 3600,
                discard=DiscardPolicy.OLD,
            )
        )
    finally:
        await nc.close()

    app = FastAPI()
    app.state.session_factory = audit_env.session_factory
    async with consumer_hook(audit_env.settings)(app):

        async def _capped() -> StreamConfig | None:
            config = await _stream_config(audit_env.nats_url, ACCESS_STREAM)
            return config if config is not None and config.max_bytes == 1024**3 else None

        access = await eventually(_capped, within_s=15)

    assert access.discard == DiscardPolicy.OLD
    assert access.max_age == 7 * 24 * 3600
    assert access.duplicate_window == 60
    assert access.subjects == ["access.>"]


async def test_an_access_event_is_stored_by_the_access_writer_with_category_access(
    audit_env: Any, run_consumer: Any, eventually: Any
) -> None:
    from dc_audit.consumer import access_consumer

    run_consumer(access_consumer(audit_env.session_factory, audit_env.settings))

    async def _ready() -> StreamConfig | None:
        return await _stream_config(audit_env.nats_url, ACCESS_STREAM)

    await eventually(_ready, within_s=15)
    envelope = _access()
    assert subject_for("contracts", "contract", "read", prefix="access") == ACCESS_SUBJECT
    publisher = audit_env.publisher(stream=ACCESS_STREAM)
    try:
        await publisher.publish(
            ACCESS_SUBJECT, encode_envelope(envelope), msg_id=envelope["event_id"]
        )

        async def _stored() -> list[dict[str, Any]]:
            return await _rows(audit_env)

        (row,) = await eventually(_stored, within_s=15)
    finally:
        await publisher.close()
    assert row["category"] == "access"
    assert row["detail"] == envelope["detail"]
    nc = await nats.connect(servers=[audit_env.nats_url])
    try:
        info = await nc.jetstream().consumer_info(ACCESS_STREAM, "audit-access-writer")
    finally:
        await nc.close()
    assert info.config.filter_subject == "access.>"


SPOOFS = [
    pytest.param(
        _change(category="denial", service="contracts", aggregate_type="contract"),
        "audit.contracts.contract.widget.created",
        id="denial-claimed-by-contracts",
    ),
    pytest.param(
        _access(), "audit.contracts.contract.read", id="access-category-on-an-audit-subject"
    ),
    pytest.param(_access(after={"name": "x"}), ACCESS_SUBJECT, id="access-with-after"),
    pytest.param(_access(before={"name": "x"}), ACCESS_SUBJECT, id="access-with-before"),
    pytest.param(_denial(before={"name": "x"}), DENIAL_SUBJECT, id="denial-with-before"),
    pytest.param(_denial(after={"name": "x"}), DENIAL_SUBJECT, id="denial-with-after"),
    pytest.param(
        _change(category="access"),
        "audit.probe.widget.widget.created",
        id="access-claimed-on-a-change-subject",
    ),
    pytest.param(_denial(category="change"), DENIAL_SUBJECT, id="denial-claimed-as-change"),
]


@pytest.mark.parametrize(("envelope", "subject"), SPOOFS)
async def test_forged_categories_and_content_are_refused(
    audit_env: Any, envelope: dict[str, Any], subject: str
) -> None:
    with pytest.raises(SubjectMismatchError):
        await handle_payload(audit_env.session_factory, encode_envelope(envelope), subject=subject)
    assert await _rows(audit_env) == []


async def test_a_forged_access_event_ends_in_the_dead_letter_not_the_store(
    audit_env: Any, run_consumer: Any, eventually: Any
) -> None:
    from dc_audit.consumer import access_consumer

    run_consumer(
        access_consumer(audit_env.session_factory, audit_env.settings, max_nak_delay_s=0.05)
    )

    async def _ready() -> StreamConfig | None:
        return await _stream_config(audit_env.nats_url, ACCESS_STREAM)

    await eventually(_ready, within_s=15)
    envelope = _access(after={"document": "smuggled"})
    publisher = audit_env.publisher(stream=ACCESS_STREAM)
    try:
        await publisher.publish(
            ACCESS_SUBJECT, encode_envelope(envelope), msg_id=envelope["event_id"]
        )

        async def _dead() -> list[dict[str, Any]]:
            return await audit_env.fetch("SELECT * FROM audit_dead_letter")

        (dead,) = await eventually(_dead, within_s=30)
    finally:
        await publisher.close()
    assert dead["subject"] == ACCESS_SUBJECT
    assert dead["error"].startswith("SubjectMismatchError")
    assert dead["deliveries"] == audit_env.settings.max_deliver
    assert await _rows(audit_env) == []


def _detail(keys: int = 16, value_chars: int = 512) -> dict[str, Any]:
    names = [f"key_{chr(ord('a') + i)}" for i in range(keys)]
    detail: dict[str, Any] = dict.fromkeys(names, 1)
    detail[names[0]] = "v" * value_chars
    detail[names[1]] = True
    detail[names[2]] = None
    return detail


async def test_a_detail_at_the_caps_is_accepted(audit_env: Any) -> None:
    envelope = _denial(detail=_detail(16, 512))
    assert await handle_payload(
        audit_env.session_factory, encode_envelope(envelope), subject=DENIAL_SUBJECT
    )
    (row,) = await _rows(audit_env)
    assert row["detail"] == envelope["detail"]


@pytest.mark.parametrize(
    "detail",
    [
        pytest.param(_detail(17, 10), id="17-keys"),
        pytest.param(_detail(3, 513), id="513-characters"),
        pytest.param({"nested": {"a": 1}}, id="nested-object"),
        pytest.param({"listed": [1, 2]}, id="list-value"),
        pytest.param({"Bad-Key": "x"}, id="key-outside-pattern"),
        pytest.param({"k" * 33: "x"}, id="key-too-long"),
        pytest.param({"": "x"}, id="empty-key"),
        pytest.param({"ratio": 1.5}, id="float-value"),
    ],
)
async def test_a_detail_outside_the_caps_is_refused(audit_env: Any, detail: Any) -> None:
    payload = encode_envelope(_denial(detail=detail))
    with pytest.raises(ValidationError):
        await handle_payload(audit_env.session_factory, payload, subject=DENIAL_SUBJECT)
    assert await _rows(audit_env) == []


@pytest.fixture
def read_app(audit_env: Any, fake_issuer: FakeIssuer) -> Any:
    from dc_audit.main import create_audit_app
    from dc_audit.settings import AuditSettings

    settings = AuditSettings(database_url=audit_env.db_url)  # type: ignore[arg-type]
    auth = AuthSettings(
        issuers=[fake_issuer.issuer_config()], identity_internal_url="http://identity:8000"
    )
    return create_audit_app(settings, auth)


async def test_the_internal_read_defaults_to_change_and_filters_by_category(
    audit_env: Any, read_app: Any, asgi_client: Any, fake_issuer: FakeIssuer
) -> None:
    change, denial, access = _change(), _denial(), _access()
    assert await handle_payload(audit_env.session_factory, encode_envelope(change))
    assert await handle_payload(
        audit_env.session_factory, encode_envelope(denial), subject=DENIAL_SUBJECT
    )
    assert await handle_payload(
        audit_env.session_factory, encode_envelope(access), subject=ACCESS_SUBJECT
    )
    headers = {"Authorization": f"Bearer {fake_issuer.mint('admin')}"}
    with respx.mock() as router:
        router.post(PDP).respond(json={"outcome": "allow", "folder": None})
        async with asgi_client(read_app) as client:
            default = await client.get(EVENTS, headers=headers)
            denials = await client.get(EVENTS, params={"category": "denial"}, headers=headers)
            accesses = await client.get(EVENTS, params={"category": "access"}, headers=headers)
            bogus = await client.get(EVENTS, params={"category": "bogus"}, headers=headers)

    assert default.status_code == 200
    assert [(e["event_id"], e["category"]) for e in default.json()] == [
        (change["event_id"], "change")
    ]
    assert default.json()[0]["detail"] is None
    (row,) = denials.json()
    assert (row["event_id"], row["category"]) == (denial["event_id"], "denial")
    assert row["detail"] == denial["detail"]
    assert [e["category"] for e in accesses.json()] == ["access"]
    assert bogus.status_code == 422
    assert bogus.headers["content-type"].startswith("application/problem+json")
