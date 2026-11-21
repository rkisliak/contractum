"""The contracts notice consumer over real NATS: relay, AUDIT stream, contracts-notices, notices.

Tracer: a deprecation published by the existing outbox relay becomes one card notice through the
in-service JetStream consumer (D-99 as amended, SUB-07).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import nats
import nats.errors
import pytest
from dc_core.testing import NATS_IMAGE
from nats.js.api import AckPolicy, ConsumerConfig, StorageType, StreamConfig
from nats.js.errors import APIError
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

pytestmark = pytest.mark.integration

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def eventually(
    probe: Callable[[], Awaitable[Any]], *, within_s: float = 20.0, interval: float = 0.2
) -> Any:
    """Retry ``probe`` until it returns something truthy."""
    deadline = time.monotonic() + within_s
    while True:
        value = await probe()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(f"condition not met within {within_s} s")
        await asyncio.sleep(interval)


async def create_audit_stream(url: str) -> None:
    """Create stream AUDIT as the audit service does (the contracts service never does)."""
    nc = await nats.connect(servers=[url])
    try:
        await nc.jetstream().add_stream(
            StreamConfig(
                name="AUDIT",
                subjects=["audit.>"],
                storage=StorageType.FILE,
                duplicate_window=120,
            )
        )
    finally:
        await nc.close()


@pytest.fixture
async def audit_stream(nats_url: str) -> AsyncIterator[str]:
    await create_audit_stream(nats_url)
    yield nats_url


async def pinned_card(stack: Any) -> dict[str, Any]:
    """orders 2.3.0 (Latest), 3.0.0 (Preview); one card pins 2.3.0."""
    chain = await stack.chain()
    contract, versions = await stack.publish_versions(chain.team, ["2.3.0", "3.0.0"])
    card = await stack.create_card(chain.team)
    pinned = await stack.subscribe(card["id"], contract, "2.3.0")
    assert pinned.status_code == 201, pinned.text
    return {"contract": contract, "versions": versions, "card": card}


async def deprecate(stack: Any, contract: str, ref: str) -> Any:
    day = (datetime.now(UTC) + timedelta(days=60)).date()
    return await stack.client.post(
        f"/api/v1/contracts/contracts/{contract}/versions/{ref}/deprecate",
        json={"sunset_date": day.isoformat()},
        headers=bearer(stack.admin_token),
    )


def notices_of(sql: Sql, stack: Any, card_id: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT * FROM card_notice WHERE card_id = %s ORDER BY id",
        card_id,
    )


async def test_a_deprecation_published_by_the_relay_becomes_a_card_notice(
    make_stack: MakeStack, audit_stream: str, sql: Sql
) -> None:
    async with make_stack(nats_url=audit_stream) as stack:
        w = await pinned_card(stack)
        response = await deprecate(stack, w["contract"], "2.3.0")
        assert response.status_code == 200, response.text

        async def _notices() -> list[dict[str, Any]]:
            return notices_of(sql, stack, w["card"]["id"])

        found = await eventually(_notices)
        (outbox,) = sql(
            stack.contracts_db_url,
            "SELECT event_id FROM outbox WHERE action = 'contract.version_deprecated'",
        )
        deprecated = [n for n in found if n["event_type"] == "contract.version.deprecated"]
        assert len(deprecated) == 1
        assert str(deprecated[0]["source_event_id"]) == str(outbox["event_id"])
        assert str(deprecated[0]["version_id"]) == w["versions"]["2.3.0"]


async def test_shutting_the_app_down_cancels_the_consumer_without_errors(
    make_stack: MakeStack, audit_stream: str
) -> None:
    async with make_stack(nats_url=audit_stream) as stack:
        consumer = stack.contracts_app.state.notice_consumer
        assert consumer is not None
        tasks = [t for t in asyncio.all_tasks() if t.get_name() == "contracts-notice-consumer"]
        assert len(tasks) == 1
        task = tasks[0]
        await asyncio.sleep(0.5)  # let it subscribe
    assert task.done()
    assert task.cancelled() or task.exception() is None


async def test_without_a_nats_url_no_consumer_is_started(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        assert getattr(stack.contracts_app.state, "notice_consumer", None) is None
        names = {t.get_name() for t in asyncio.all_tasks()}
        assert "contracts-notice-consumer" not in names


# --- Task 2: redelivery, replay, poison, handler failures and a missing stream ---

DURABLE = "contracts-notices"


class Recorder:
    """Stands in for the module logger so the tests can read exactly what is logged."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def _add(self, level: str, event: str, **kwargs: Any) -> None:
        self.records.append((level, event, kwargs))

    def info(self, event: str, **kwargs: Any) -> None:
        self._add("info", event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        self._add("warning", event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        self._add("error", event, **kwargs)

    def events(self, name: str) -> list[dict[str, Any]]:
        return [kwargs for _, event, kwargs in self.records if event == name]


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    from dc_contracts import notice_consumer

    recorder = Recorder()
    monkeypatch.setattr(notice_consumer, "_log", recorder)
    return recorder


@pytest.fixture
def handled(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Every ``handle_payload`` call the consumer makes: (subject, deliveries so far)."""
    from dc_contracts import notice_consumer

    calls: list[tuple[str, int]] = []
    real = notice_consumer.handle_payload

    async def _counting(*args: Any, **kwargs: Any) -> int:
        calls.append((kwargs["subject"], len(calls) + 1))
        return await real(*args, **kwargs)

    monkeypatch.setattr(notice_consumer, "handle_payload", _counting)
    return calls


def consumer_for(stack: Any, url: str, **overrides: Any) -> Any:
    from dc_contracts.notice_consumer import NoticeConsumer

    settings = stack.contracts_app.state.settings.model_copy(
        update={
            "nats_url": url,
            "notice_consumer_ack_wait_s": 1,
            "notice_consumer_max_backoff_s": 0.5,
            **overrides,
        }
    )
    return NoticeConsumer(stack.contracts_app.state.session_factory, settings)


async def outbox_envelopes(stack: Any) -> list[tuple[str, bytes, str]]:
    """(subject, payload, event id) of every contracts outbox row, as the relay would publish."""
    from dc_contracts.models import OutboxEvent
    from dc_core.events import build_envelope, encode_envelope, subject_for
    from sqlalchemy import select

    async with stack.contracts_app.state.session_factory() as session:
        rows = (await session.execute(select(OutboxEvent).order_by(OutboxEvent.id))).scalars().all()
    return [
        (
            subject_for("contracts", row.aggregate_type, row.action),
            encode_envelope(build_envelope(row, service="contracts")),
            str(row.event_id),
        )
        for row in rows
    ]


async def publish_all(stack: Any, url: str) -> int:
    """Publish every outbox row to the AUDIT stream with a plain client; returns how many."""
    envelopes = await outbox_envelopes(stack)
    nc = await nats.connect(servers=[url])
    try:
        js = nc.jetstream()
        for subject, payload, event_id in envelopes:
            await js.publish(subject, payload, headers={"Nats-Msg-Id": event_id})
    finally:
        await nc.close()
    return len(envelopes)


async def publish_raw(url: str, subject: str, payload: bytes) -> None:
    nc = await nats.connect(servers=[url])
    try:
        await nc.jetstream().publish(subject, payload)
    finally:
        await nc.close()


async def drain(consumer: Any, *, passes: int = 6) -> int:
    """Consume until a fetch comes back empty; returns the messages handled."""
    total = 0
    for _ in range(passes):
        handled = await consumer.consume_once(timeout_s=1.0)
        if handled == 0:
            return total
        total += handled
    return total


def count(sql: Sql, stack: Any, table: str) -> int:
    return int(sql(stack.contracts_db_url, f"SELECT count(*) AS n FROM {table}")[0]["n"])  # noqa: S608


async def consumer_info(url: str) -> Any:
    nc = await nats.connect(servers=[url])
    try:
        return await nc.jetstream().consumer_info("AUDIT", DURABLE)
    finally:
        await nc.close()


async def test_a_redelivered_message_adds_no_notice_and_no_delivery_row(
    make_stack: MakeStack, audit_stream: str, sql: Sql, log: Recorder, handled: list[Any]
) -> None:
    async with make_stack() as stack:
        w = await pinned_card(stack)
        assert (await deprecate(stack, w["contract"], "2.3.0")).status_code == 200
        published = await publish_all(stack, audit_stream)
        consumer = consumer_for(stack, audit_stream)
        try:
            real_process = consumer._process

            async def _withhold_ack(msg: Any) -> None:
                async def _no_ack() -> None:
                    return None

                msg.ack = _no_ack
                await real_process(msg)

            consumer._process = _withhold_ack
            assert await consumer.consume_once(timeout_s=2.0) == published
            first_pass = len(handled)
            notices = count(sql, stack, "card_notice")
            deliveries = count(sql, stack, "card_notice_delivery")
            assert notices >= 1

            consumer._process = real_process
            await asyncio.sleep(1.5)  # longer than the 1 s ack wait: the server redelivers
            assert await drain(consumer) == published
            assert len(handled) == first_pass * 2
            assert count(sql, stack, "card_notice") == notices
            assert count(sql, stack, "card_notice_delivery") == deliveries

            info = await consumer_info(audit_stream)
            assert info.num_pending == 0
            assert info.num_ack_pending == 0
        finally:
            await consumer.close()


async def test_a_full_replay_from_a_fresh_durable_adds_no_notice_and_no_delivery_row(
    make_stack: MakeStack, audit_stream: str, sql: Sql, log: Recorder, handled: list[Any]
) -> None:
    async with make_stack() as stack:
        w = await pinned_card(stack)
        assert (await deprecate(stack, w["contract"], "2.3.0")).status_code == 200
        published = await publish_all(stack, audit_stream)
        first = consumer_for(stack, audit_stream)
        try:
            assert await drain(first) == published
        finally:
            await first.close()
        notices = count(sql, stack, "card_notice")
        deliveries = count(sql, stack, "card_notice_delivery")
        assert notices >= 1
        calls_before = len(handled)

        nc = await nats.connect(servers=[audit_stream])
        try:
            assert await nc.jetstream().delete_consumer("AUDIT", DURABLE)
        finally:
            await nc.close()

        second = consumer_for(stack, audit_stream)
        try:
            assert await drain(second) == published
        finally:
            await second.close()
        assert len(handled) - calls_before == published  # every stored event handled again
        assert count(sql, stack, "card_notice") == notices
        assert count(sql, stack, "card_notice_delivery") == deliveries


SPOOFED = (
    b'{"event_id":"0198f3a0-7b6c-7000-8000-000000000001","occurred_at":"2026-11-12T10:00:00Z",'
    b'"service":"identity","aggregate_type":"contract",'
    b'"aggregate_id":"0198f3a0-7b6c-7000-8000-000000000002",'
    b'"action":"contract.version_deprecated","after":{"notify_card_ids":[]}}'
)


async def test_a_spoofed_envelope_is_logged_and_acknowledged_on_its_first_delivery(
    make_stack: MakeStack, audit_stream: str, sql: Sql, log: Recorder, handled: list[Any]
) -> None:
    async with make_stack() as stack:
        await publish_raw(
            audit_stream, "audit.contracts.contract.contract.version_deprecated", SPOOFED
        )
        consumer = consumer_for(stack, audit_stream)
        try:
            assert await consumer.consume_once(timeout_s=2.0) == 1
            assert await consumer.consume_once(timeout_s=1.5) == 0  # nothing is redelivered
            assert len(handled) == 1
            invalid = log.events("notice_event_invalid")
            assert len(invalid) == 1
            assert invalid[0]["subject"] == "audit.contracts.contract.contract.version_deprecated"
            assert invalid[0]["error"] == "InvalidNoticeEvent"
            assert count(sql, stack, "card_notice") == 0
            info = await consumer_info(audit_stream)
            assert info.num_pending == 0
            assert info.num_ack_pending == 0
            assert info.delivered.stream_seq == 1
        finally:
            await consumer.close()


async def test_a_failing_handler_is_naked_with_backoff_and_recorded_once_it_succeeds(
    make_stack: MakeStack,
    audit_stream: str,
    sql: Sql,
    log: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dc_contracts import notice_consumer

    async with make_stack() as stack:
        w = await pinned_card(stack)
        assert (await deprecate(stack, w["contract"], "2.3.0")).status_code == 200
        real = notice_consumer.handle_payload
        failures: list[int] = []

        async def _flaky(*args: Any, **kwargs: Any) -> int:
            if kwargs["subject"].endswith("version_deprecated") and len(failures) < 2:
                failures.append(1)
                raise RuntimeError("database is gone: secret payload text")
            return await real(*args, **kwargs)

        monkeypatch.setattr(notice_consumer, "handle_payload", _flaky)
        await publish_all(stack, audit_stream)
        consumer = consumer_for(stack, audit_stream)
        try:

            async def _done() -> bool:
                await consumer.consume_once(timeout_s=1.0)
                return bool(notices_of(sql, stack, w["card"]["id"]))

            await eventually(_done, within_s=30.0)
        finally:
            await consumer.close()
        warned = log.events("notice_event_retry")
        assert [item["deliveries"] for item in warned] == [1, 2]
        assert len(notices_of(sql, stack, w["card"]["id"])) == 1
        assert "secret payload text" not in repr(log.records)


async def test_after_the_maximum_deliveries_a_message_is_logged_and_acknowledged(
    make_stack: MakeStack,
    audit_stream: str,
    sql: Sql,
    log: Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dc_contracts import notice_consumer

    async with make_stack() as stack:
        w = await pinned_card(stack)
        assert (await deprecate(stack, w["contract"], "2.3.0")).status_code == 200
        calls: list[int] = []

        async def _always_fails(*args: Any, **kwargs: Any) -> int:
            calls.append(1)
            raise RuntimeError("database is gone: secret payload text")

        monkeypatch.setattr(notice_consumer, "handle_payload", _always_fails)
        envelopes = await outbox_envelopes(stack)
        deprecated = [e for e in envelopes if e[0].endswith("version_deprecated")]
        assert len(deprecated) == 1
        subject, payload, event_id = deprecated[0]
        await publish_raw(audit_stream, subject, payload)
        consumer = consumer_for(stack, audit_stream, notice_consumer_max_deliver=2)
        try:

            async def _dropped() -> bool:
                await consumer.consume_once(timeout_s=1.0)
                return bool(log.events("notice_event_dropped"))

            await eventually(_dropped, within_s=30.0)
            assert await consumer.consume_once(timeout_s=1.5) == 0
        finally:
            await consumer.close()
        assert len(calls) == 2
        (dropped,) = log.events("notice_event_dropped")
        assert dropped["event_id"] == event_id
        assert dropped["subject"] == subject
        assert dropped["error"] == "RuntimeError"
        assert count(sql, stack, "card_notice") == 0
        assert "secret payload text" not in repr(log.records)
        assert w["contract"] not in repr(log.records)
        info = await consumer_info(audit_stream)
        assert info.num_pending == 0
        assert info.num_ack_pending == 0


async def test_a_missing_stream_is_retried_and_events_are_handled_once_it_exists(
    make_stack: MakeStack, nats_url: str, sql: Sql, log: Recorder
) -> None:
    async with make_stack() as stack:
        w = await pinned_card(stack)
        assert (await deprecate(stack, w["contract"], "2.3.0")).status_code == 200
        consumer = consumer_for(stack, nats_url)  # AUDIT does not exist yet
        task = asyncio.create_task(consumer.run())
        try:
            await asyncio.sleep(1.5)
            assert not task.done()  # no exception escaped run()
            assert log.events("notice_consumer_loop_error")
            await create_audit_stream(nats_url)
            await publish_all(stack, nats_url)

            async def _notices() -> list[dict[str, Any]]:
                return notices_of(sql, stack, w["card"]["id"])

            assert len(await eventually(_notices, within_s=30.0)) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_a_deprecate_request_never_waits_for_the_consumer_or_the_broker(
    make_stack: MakeStack, nats_url: str, sql: Sql
) -> None:
    # AUDIT does not exist: the relay and the consumer both retry in the background.
    async with make_stack(nats_url=nats_url) as stack:
        w = await pinned_card(stack)
        started = time.monotonic()
        response = await deprecate(stack, w["contract"], "2.3.0")
        assert response.status_code == 200, response.text
        assert time.monotonic() - started < 5.0
        assert notices_of(sql, stack, w["card"]["id"]) == []


# --- Task 3: a NATS server started with the rendered template enforces the least privilege ---

REPO = Path(__file__).resolve().parents[3]
SERVICE_USERS = ("identity", "contracts", "validation", "audit")
PASSWORDS = {name: f"{name}-test-password-0123456789" for name in SERVICE_USERS}
FILTER = "audit.contracts.>"
CREATE_ALLOWED = f"$JS.API.CONSUMER.CREATE.AUDIT.{DURABLE}.{FILTER}"
EVENT_SUBJECT = "audit.contracts.contract.contract.created"  # an action notices ignore


def rendered_config() -> bytes:
    text = (REPO / "infra" / "nats" / "nats.conf.tmpl").read_text(encoding="utf-8")
    for name, password in PASSWORDS.items():
        text = text.replace("{{ " + name + "_nats_password }}", password)
    assert "{{" not in text
    return text.encode("utf-8")


def start_secured_server() -> DockerContainer:
    container = (
        DockerContainer(NATS_IMAGE)
        .with_command("-c /etc/nats/nats.conf")
        .with_exposed_ports(4222)
        .with_copy_into_container(rendered_config(), "/etc/nats/nats.conf")
        .waiting_for(LogMessageWaitStrategy("Server is ready").with_startup_timeout(60))
    )
    container.start()
    return container


def address(container: DockerContainer) -> str:
    return f"nats://{container.get_container_host_ip()}:{container.get_exposed_port(4222)}"


@dataclass
class Connection:
    nc: Any
    errors: list[str]

    def violations(self, since: int, subject: str) -> list[str]:
        """Permission violations since ``since`` naming ``subject`` (nats-py lower-cases them)."""
        return [
            error
            for error in self.errors[since:]
            if "permissions violation" in error.lower() and subject.lower() in error.lower()
        ]


async def connect_as(url: str, user: str) -> Connection:
    errors: list[str] = []

    async def _on_error(exc: Exception) -> None:
        errors.append(str(exc))

    nc = await nats.connect(servers=[url], user=user, password=PASSWORDS[user], error_cb=_on_error)
    return Connection(nc=nc, errors=errors)


@pytest.fixture(scope="module")
def secured_server() -> Iterator[DockerContainer]:
    container = start_secured_server()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def fresh_secured_server() -> Iterator[DockerContainer]:
    container = start_secured_server()
    try:
        yield container
    finally:
        container.stop()


async def create_audit_stream_as_audit(url: str) -> None:
    audit = await connect_as(url, "audit")
    try:
        await audit.nc.jetstream().add_stream(
            StreamConfig(
                name="AUDIT",
                subjects=["audit.>"],
                storage=StorageType.FILE,
                duplicate_window=120,
            )
        )
    finally:
        await audit.nc.close()


@dataclass
class Observed:
    """What the audit user sees on the server (the only user that may look)."""

    messages: int
    consumers: dict[str, Any]
    streams: list[str]


async def observe(url: str) -> Observed:
    audit = await connect_as(url, "audit")
    try:
        js = audit.nc.jetstream()
        info = await js.stream_info("AUDIT")
        consumers = {c.name: c.config for c in await js.consumers_info("AUDIT")}
        names = [s.config.name for s in await js.streams_info()]
        return Observed(messages=info.state.messages, consumers=consumers, streams=names)
    finally:
        await audit.nc.close()


def assert_filter_intact(seen: Observed) -> None:
    config = seen.consumers[DURABLE]
    assert config.filter_subject == FILTER
    assert not config.filter_subjects


async def refused(connection: Connection, call: Callable[[], Awaitable[Any]], subject: str) -> str:
    """Run ``call`` as a user; it passes only when the server genuinely refused it.

    A refusal is either a permissions violation naming ``subject`` or a JetStream API error from
    the server (other than "not found"). Anything the server accepts fails the test, and a bare
    timeout is not a refusal.
    """
    since = len(connection.errors)
    try:
        result = await call()
    except APIError as exc:
        assert exc.code != 404, f"{subject}: not a refusal, {exc}"
        return f"api error {exc.code}/{exc.err_code}: {exc.description}"
    except (nats.errors.TimeoutError, TimeoutError):
        for _ in range(30):
            hits = connection.violations(since, subject)
            if hits:
                return hits[0]
            await asyncio.sleep(0.1)
        pytest.fail(f"timeout without a permissions violation naming {subject}")
    pytest.fail(f"the server accepted a request that must be refused: {subject} -> {result!r}")


def raw_create(connection: Connection, config: dict[str, Any]) -> Callable[[], Awaitable[Any]]:
    """A hand-built create request on the one allowed subject, with an arbitrary config body."""
    body = json.dumps({"stream_name": "AUDIT", "config": config}).encode()

    async def _call() -> Any:
        msg = await connection.nc.request(CREATE_ALLOWED, body, timeout=1.5)
        response = json.loads(msg.data)
        if "error" in response:
            raise APIError.from_error(response["error"])
        return response

    return _call


def consumer_config(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"name": DURABLE, "durable_name": DURABLE, "ack_policy": "explicit"}
    base.update(fields)
    return base


class _NeverUsed:
    """The session factory of a consumer whose events never reach a handler with a database."""

    def __call__(self) -> Any:  # pragma: no cover - the positive control uses an ignored action
        raise AssertionError("the positive control must not touch the database")


@dataclass
class Provisioned:
    url: str
    handled: int
    error: str | None = None

    def require(self) -> None:
        """The positive control must have passed before any refusal means anything."""
        assert self.error is None, f"the legitimate consumer did not work: {self.error}"
        assert self.handled == 1


@pytest.fixture(scope="module")
async def provisioned(secured_server: DockerContainer) -> Provisioned:
    """AUDIT created by audit; then, as contracts, the consumer is created, fetches and acks."""
    from dc_contracts.notice_consumer import NoticeConsumer
    from dc_contracts.settings import ContractsSettings
    from dc_core.events import JetStreamPublisher
    from pydantic import SecretStr

    url = address(secured_server)
    await create_audit_stream_as_audit(url)
    publisher = JetStreamPublisher(url, user="contracts", password=PASSWORDS["contracts"])
    envelope = (
        b'{"event_id":"0198f3a0-7b6c-7000-8000-0000000000aa",'
        b'"occurred_at":"2026-11-12T10:00:00Z","service":"contracts",'
        b'"aggregate_type":"contract","aggregate_id":"0198f3a0-7b6c-7000-8000-0000000000bb",'
        b'"action":"contract.created"}'
    )
    try:
        await publisher.publish(
            EVENT_SUBJECT, envelope, msg_id="0198f3a0-7b6c-7000-8000-0000000000aa"
        )
    finally:
        await publisher.close()
    settings = ContractsSettings(
        database_url=SecretStr("postgresql+psycopg://unused:unused@localhost/unused"),  # type: ignore[arg-type]
        nats_url=url,
        nats_user="contracts",
        nats_password=SecretStr(PASSWORDS["contracts"]),
        notice_consumer_ack_wait_s=5,
    )
    consumer = NoticeConsumer(_NeverUsed(), settings)  # type: ignore[arg-type]
    try:
        handled = await consumer.consume_once(timeout_s=3.0)
    except Exception as exc:
        return Provisioned(url=url, handled=0, error=f"{type(exc).__name__}: {exc}")
    finally:
        await consumer.close()
    return Provisioned(url=url, handled=handled)


async def test_the_contracts_user_creates_fetches_and_acks_its_one_consumer(
    provisioned: Provisioned,
) -> None:
    provisioned.require()
    audit = await connect_as(provisioned.url, "audit")
    try:
        info = await audit.nc.jetstream().consumer_info("AUDIT", DURABLE)
    finally:
        await audit.nc.close()
    assert info.config.filter_subject == FILTER
    assert not info.config.filter_subjects
    assert info.num_pending == 0
    assert info.num_ack_pending == 0
    assert info.ack_floor.stream_seq == 1
    assert info.delivered.stream_seq == 1


async def test_a_consumer_with_a_wider_filter_cannot_be_created_on_a_fresh_server(
    fresh_secured_server: DockerContainer,
) -> None:
    url = address(fresh_secured_server)
    await create_audit_stream_as_audit(url)
    contracts = await connect_as(url, "contracts")
    try:
        js = contracts.nc.jetstream(timeout=1.5)
        subject = f"$JS.API.CONSUMER.CREATE.AUDIT.{DURABLE}.audit.identity.>"
        how = await refused(
            contracts,
            lambda: js.add_consumer(
                "AUDIT",
                ConsumerConfig(
                    name=DURABLE,
                    durable_name=DURABLE,
                    ack_policy=AckPolicy.EXPLICIT,
                    filter_subject="audit.identity.>",
                ),
            ),
            subject,
        )
        print("fresh create with another filter:", how)
    finally:
        await contracts.nc.close()
    seen = await observe(url)
    assert DURABLE not in seen.consumers
    assert seen.consumers == {}


async def test_a_consumer_with_another_durable_name_cannot_be_created(
    fresh_secured_server: DockerContainer,
) -> None:
    url = address(fresh_secured_server)
    contracts = await connect_as(url, "contracts")
    try:
        js = contracts.nc.jetstream(timeout=1.5)
        how = await refused(
            contracts,
            lambda: js.add_consumer(
                "AUDIT",
                ConsumerConfig(
                    name="other",
                    durable_name="other",
                    ack_policy=AckPolicy.EXPLICIT,
                    filter_subject=FILTER,
                ),
            ),
            f"$JS.API.CONSUMER.CREATE.AUDIT.other.{FILTER}",
        )
        print("create with another durable name:", how)
    finally:
        await contracts.nc.close()
    seen = await observe(url)
    assert seen.consumers == {}


async def test_updating_the_existing_consumer_to_a_wider_filter_is_refused(
    provisioned: Provisioned,
) -> None:
    provisioned.require()
    before = await observe(provisioned.url)
    contracts = await connect_as(provisioned.url, "contracts")
    try:
        js = contracts.nc.jetstream(timeout=1.5)
        how = await refused(
            contracts,
            lambda: js.add_consumer(
                "AUDIT",
                ConsumerConfig(
                    name=DURABLE,
                    durable_name=DURABLE,
                    ack_policy=AckPolicy.EXPLICIT,
                    filter_subject="audit.identity.>",
                ),
            ),
            f"$JS.API.CONSUMER.CREATE.AUDIT.{DURABLE}.audit.identity.>",
        )
        print("update to filter audit.identity.>:", how)
    finally:
        await contracts.nc.close()
    after = await observe(provisioned.url)
    assert_filter_intact(after)
    assert after.messages == before.messages


@pytest.mark.parametrize(
    "case, fields",
    [
        (
            "body filter_subject differs from the subject token",
            {"filter_subject": "audit.identity.>"},
        ),
        ("body uses filter_subjects", {"filter_subjects": ["audit.identity.>"]}),
        (
            "body filter_subject matches but filter_subjects widens",
            {"filter_subject": FILTER, "filter_subjects": ["audit.identity.>"]},
        ),
    ],
)
async def test_a_create_on_the_allowed_subject_cannot_widen_the_filter_through_its_body(
    provisioned: Provisioned, case: str, fields: dict[str, Any]
) -> None:
    provisioned.require()
    before = await observe(provisioned.url)
    contracts = await connect_as(provisioned.url, "contracts")
    try:
        how = await refused(
            contracts, raw_create(contracts, consumer_config(**fields)), CREATE_ALLOWED
        )
        print(f"{case}:", how)
    finally:
        await contracts.nc.close()
    after = await observe(provisioned.url)
    assert_filter_intact(after)
    assert after.messages == before.messages


async def test_contracts_cannot_delete_purge_or_add_streams(provisioned: Provisioned) -> None:
    provisioned.require()
    before = await observe(provisioned.url)
    contracts = await connect_as(provisioned.url, "contracts")
    try:
        js = contracts.nc.jetstream(timeout=1.5)
        print(
            "delete AUDIT:",
            await refused(
                contracts, lambda: js.delete_stream("AUDIT"), "$JS.API.STREAM.DELETE.AUDIT"
            ),
        )
        print(
            "purge AUDIT:",
            await refused(
                contracts, lambda: js.purge_stream("AUDIT"), "$JS.API.STREAM.PURGE.AUDIT"
            ),
        )
        print(
            "add stream X:",
            await refused(
                contracts,
                lambda: js.add_stream(name="X", subjects=["x.>"]),
                "$JS.API.STREAM.CREATE.X",
            ),
        )
    finally:
        await contracts.nc.close()
    after = await observe(provisioned.url)
    assert after.streams == ["AUDIT"]
    assert after.messages == before.messages
    assert_filter_intact(after)


async def test_contracts_cannot_read_a_stored_message_by_sequence(
    provisioned: Provisioned,
) -> None:
    provisioned.require()
    contracts = await connect_as(provisioned.url, "contracts")
    try:
        js = contracts.nc.jetstream(timeout=1.5)
        print(
            "get_msg direct:",
            await refused(
                contracts,
                lambda: js.get_msg("AUDIT", 1, direct=True),
                "$JS.API.DIRECT.GET.AUDIT",
            ),
        )
        print(
            "get_msg:",
            await refused(
                contracts,
                lambda: js.get_msg("AUDIT", 1),
                "$JS.API.STREAM.MSG.GET.AUDIT",
            ),
        )
    finally:
        await contracts.nc.close()


async def test_contracts_cannot_subscribe_to_audit_events_directly(
    provisioned: Provisioned,
) -> None:
    provisioned.require()
    contracts = await connect_as(provisioned.url, "contracts")
    received: list[bytes] = []

    async def _on_message(msg: Any) -> None:
        received.append(msg.data)

    try:
        since = len(contracts.errors)
        await contracts.nc.subscribe("audit.>", cb=_on_message)
        await contracts.nc.flush()
        # contracts publishes a further event; a subscription that worked would see it
        publisher = contracts.nc.jetstream(timeout=3.0)
        await publisher.publish(
            EVENT_SUBJECT,
            b'{"event_id":"0198f3a0-7b6c-7000-8000-0000000000cc"}',
            headers={"Nats-Msg-Id": "0198f3a0-7b6c-7000-8000-0000000000cc"},
        )
        for _ in range(30):
            if contracts.violations(since, "audit.>"):
                break
            await asyncio.sleep(0.1)
        assert contracts.violations(since, "audit.>")
        assert received == []
    finally:
        await contracts.nc.close()


# --- Phase 7 (D-171): contracts publishes its own access events, and nothing else on access. ---

ACCESS_SUBJECT = "access.contracts.contract.read"


@pytest.fixture(scope="module")
async def access_stream(secured_server: DockerContainer) -> str:
    """Stream ACCESS created by the audit user, as the audit service does."""
    url = address(secured_server)
    audit = await connect_as(url, "audit")
    try:
        await audit.nc.jetstream().add_stream(
            StreamConfig(
                name="ACCESS",
                subjects=["access.>"],
                storage=StorageType.FILE,
                duplicate_window=120,
            )
        )
    finally:
        await audit.nc.close()
    return url


async def access_messages(url: str) -> int:
    audit = await connect_as(url, "audit")
    try:
        return int((await audit.nc.jetstream().stream_info("ACCESS")).state.messages)
    finally:
        await audit.nc.close()


async def test_contracts_publishes_its_access_events_on_the_access_stream(
    access_stream: str,
) -> None:
    from dc_core.events import ACCESS_STREAM, JetStreamPublisher

    before = await access_messages(access_stream)
    publisher = JetStreamPublisher(
        access_stream, user="contracts", password=PASSWORDS["contracts"], stream=ACCESS_STREAM
    )
    try:
        ack = await publisher.publish(
            ACCESS_SUBJECT,
            b'{"event_id":"0198f3a0-7b6c-7000-8000-0000000000dd"}',
            msg_id="0198f3a0-7b6c-7000-8000-0000000000dd",
        )
    finally:
        await publisher.close()
    assert ack.duplicate is False
    assert await access_messages(access_stream) == before + 1


@pytest.mark.parametrize("subject", ["access.identity.x", "access.audit.y"])
async def test_contracts_cannot_publish_another_services_access_events(
    access_stream: str, subject: str
) -> None:
    before = await access_messages(access_stream)
    contracts = await connect_as(access_stream, "contracts")
    try:
        js = contracts.nc.jetstream(timeout=1.5)
        await refused(contracts, lambda: js.publish(subject, b"{}", stream="ACCESS"), subject)
    finally:
        await contracts.nc.close()
    assert await access_messages(access_stream) == before
