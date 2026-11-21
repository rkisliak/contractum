"""The webhook delivery worker (SUB-07, D-98, D-99): signed requests, recorded results.

A card set to webhook receives each recorded notice as a Standard Webhooks request from the
in-service worker. Tests drive ``app.state.card_notifier.run_once()`` against a local HTTP sink;
``make_stack`` keeps the background loop off so each pass is deterministic.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from dc_contracts import webhooks

pytestmark = pytest.mark.integration

CARDS = "/api/v1/contracts/cards"
KEY = Fernet.generate_key().decode()

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

SETTINGS: dict[str, Any] = {
    "webhook_secret_key": KEY,
    "webhook_require_https": False,
    "webhook_allowed_private_hosts": ["localhost"],
    # The worker reads the mandatory allow-list from identity with the notifier key.
    "with_notifier": True,
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one resolver seam: ``localhost`` is the sink, nothing else resolves."""

    async def fake(host: str, port: int) -> list[str]:
        if host == "localhost":
            return ["127.0.0.1"]
        raise OSError("no such host")

    monkeypatch.setattr(webhooks, "_resolve", fake)


def key_of(secret: str) -> bytes:
    return base64.b64decode(secret.removeprefix("whsec_"), validate=True)


def signature_ok(secret: str, request: Any) -> bool:
    """Does one of the request's v1 entries verify against ``secret`` over the exact bytes?"""
    msg_id = request.headers["webhook-id"]
    stamp = request.headers["webhook-timestamp"]
    mac = hmac.new(
        key_of(secret), f"{msg_id}.{stamp}.".encode() + request.body, hashlib.sha256
    ).digest()
    wanted = "v1," + base64.b64encode(mac).decode()
    entries = request.headers["webhook-signature"].split(" ")
    return any(hmac.compare_digest(wanted, entry) for entry in entries)


def today_plus(days: int) -> Any:
    return (datetime.now(UTC) + timedelta(days=days)).date()


async def webhook_world(stack: Any, sink: Any, *, path: str = "/hooks") -> dict[str, Any]:
    """Contract 2.3.0 (Latest) and 3.0.0; a webhook card (URL on the sink) pins 2.3.0."""
    chain = await stack.chain()
    contract, versions = await stack.publish_versions(chain.team, ["2.3.0", "3.0.0"])
    token, principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal, "consumer", chain.team)
    card = await stack.create_card(chain.team, token=token, name="orders consumers")
    enabled = await stack.client.patch(
        f"{CARDS}/{card['id']}",
        json={"webhook_url": sink.url(path), "webhook_enabled": True},
        headers=bearer(token),
    )
    assert enabled.status_code == 200, enabled.text
    pinned = await stack.subscribe(card["id"], contract, "2.3.0")
    assert pinned.status_code == 201, pinned.text
    return {
        "chain": chain,
        "contract": contract,
        "versions": versions,
        "card": card,
        "manager": token,
        "secret": enabled.json()["webhook_secret"],
    }


async def deprecate(stack: Any, contract: str, ref: str = "2.3.0") -> httpx.Response:
    return await stack.client.post(
        f"/api/v1/contracts/contracts/{contract}/versions/{ref}/deprecate",
        json={"sunset_date": today_plus(60).isoformat()},
        headers=bearer(stack.admin_token),
    )


def deliveries(sql: Sql, stack: Any, card_id: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT d.* FROM card_notice_delivery d JOIN card_notice n ON n.id = d.notice_id "
        "WHERE n.card_id = %s AND d.channel = 'webhook' ORDER BY d.id",
        card_id,
    )


def notifier(stack: Any) -> Any:
    return stack.contracts_app.state.card_notifier


# --- tracer: a card set to webhook receives a deprecation as a signed request ------------------


async def test_a_webhook_card_receives_a_deprecation_as_a_signed_request(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    async with make_stack(**SETTINGS) as stack:
        w = await webhook_world(stack, webhook_sink)
        response = await deprecate(stack, w["contract"])
        assert response.status_code == 200, response.text
        assert await stack.pump_notices() == 1

        (pending,) = deliveries(sql, stack, w["card"]["id"])
        assert pending["status"] == "pending"
        assert await notifier(stack).run_once() == 1

        (request,) = webhook_sink.requests
        assert request.method == "POST"
        assert request.path == "/hooks"
        assert request.headers["content-type"] == "application/json"
        assert request.headers["webhook-id"] == f"msg_{pending['id']}"
        assert abs(int(request.headers["webhook-timestamp"]) - time.time()) <= 5
        assert request.headers["webhook-signature"].startswith("v1,")
        assert signature_ok(w["secret"], request)

        body = json.loads(request.body)
        assert body["type"] == "contract.version.deprecated"
        assert body["data"]["version"]["semver"] == "2.3.0"
        assert body["data"]["schema_version"] == 1
        assert body["data"]["card"] == {"id": w["card"]["id"], "name": "orders consumers"}
        assert "contacts" not in request.body.decode()


async def test_the_delivery_status_is_on_the_card(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    async with make_stack(**SETTINGS) as stack:
        w = await webhook_world(stack, webhook_sink)
        await deprecate(stack, w["contract"])
        await stack.pump_notices()
        assert await notifier(stack).run_once() == 1

        listed = await stack.client.get(
            f"{CARDS}/{w['card']['id']}/notices", headers=bearer(w["manager"])
        )
        assert listed.status_code == 200, listed.text
        (notice,) = listed.json()["items"]
        (delivery,) = [d for d in notice["deliveries"] if d["channel"] == "webhook"]
        assert delivery["status"] == "delivered"
        assert delivery["attempts"] == 1
        assert delivery["last_status"] == 200
        assert delivery["delivered_at"] is not None
        assert delivery["last_error"] is None


async def test_a_second_pass_sends_nothing_more(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    async with make_stack(**SETTINGS) as stack:
        w = await webhook_world(stack, webhook_sink)
        await deprecate(stack, w["contract"])
        await stack.pump_notices()
        assert await notifier(stack).run_once() == 1
        assert await notifier(stack).run_once() == 0
        assert len(webhook_sink.requests) == 1


# --- retries, the cap, never blocking, never disabling, concurrency, overlap signatures --------

FAST_RETRY: dict[str, Any] = {
    **SETTINGS,
    "notify_retry_schedule_s": [1, 1, 1],
    "notify_max_attempts": 4,
}


def wait_seconds(sql: Sql, stack: Any, delivery_id: Any) -> float:
    row = sql(
        stack.contracts_db_url,
        "SELECT extract(epoch FROM next_attempt_at - now()) AS wait "
        "FROM card_notice_delivery WHERE id = %s",
        delivery_id,
    )[0]
    return float(row["wait"])


def make_due(sql: Sql, stack: Any, delivery_id: Any) -> None:
    sql(
        stack.contracts_db_url,
        "UPDATE card_notice_delivery SET next_attempt_at = now() WHERE id = %s",
        delivery_id,
    )


def seed_pending(sql: Sql, stack: Any, world: dict[str, Any], count: int) -> list[str]:
    """Insert ``count`` notices with a pending webhook delivery each; returns the delivery ids."""
    ids: list[str] = []
    for index in range(count):
        notice = sql(
            stack.contracts_db_url,
            "INSERT INTO card_notice (card_id, event_type, contract_id, payload, dedupe_key, "
            "occurred_at) VALUES (%s, 'contract.version.deprecated', %s, %s::jsonb, %s, now()) "
            "RETURNING id",
            world["card"]["id"],
            world["contract"],
            json.dumps({"contract": {"id": world["contract"], "odcs_id": "urn:x"}}),
            f"seed:{uuid.uuid4()}:{index}",
        )[0]["id"]
        delivery = sql(
            stack.contracts_db_url,
            "INSERT INTO card_notice_delivery (notice_id, channel) VALUES (%s, 'webhook') "
            "RETURNING id",
            notice,
        )[0]["id"]
        ids.append(str(delivery))
    return ids


def only_row(sql: Sql, stack: Any, card_id: str) -> dict[str, Any]:
    (row,) = deliveries(sql, stack, card_id)
    return row


async def test_a_failing_receiver_is_retried_on_the_schedule_until_it_answers(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    async with make_stack(**FAST_RETRY) as stack:
        w = await webhook_world(stack, webhook_sink)
        await deprecate(stack, w["contract"])
        await stack.pump_notices()
        webhook_sink.respond_json(500, {"error": "boom-body-must-not-be-stored"})
        webhook_sink.fail_next(1, status=500)

        assert await notifier(stack).run_once() == 1
        row = only_row(sql, stack, w["card"]["id"])
        assert row["status"] == "pending"
        assert row["attempts"] == 1
        assert row["last_status"] == 500
        assert row["last_error"] == "http-500"
        assert row["delivered_at"] is None
        assert 0.5 <= wait_seconds(sql, stack, row["id"]) <= 1.3

        # Not due yet: a pass sends nothing.
        assert await notifier(stack).run_once() == 0
        assert len(webhook_sink.requests) == 1

        await asyncio.sleep(1.1)
        make_due(sql, stack, row["id"])
        assert await notifier(stack).run_once() == 1
        assert only_row(sql, stack, w["card"]["id"])["attempts"] == 2

        await asyncio.sleep(1.1)
        make_due(sql, stack, row["id"])
        assert await notifier(stack).run_once() == 1
        done = only_row(sql, stack, w["card"]["id"])
        assert done["status"] == "delivered"
        assert done["attempts"] == 3
        assert done["last_status"] == 200
        assert done["last_error"] is None

        assert len(webhook_sink.requests) == 3
        assert {r.headers["webhook-id"] for r in webhook_sink.requests} == {f"msg_{row['id']}"}
        stamps = [r.headers["webhook-timestamp"] for r in webhook_sink.requests]
        assert len(set(stamps)) == 3
        assert all(signature_ok(w["secret"], r) for r in webhook_sink.requests)
        assert len({r.body for r in webhook_sink.requests}) == 1


async def test_no_response_body_or_url_is_ever_stored(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    async with make_stack(**FAST_RETRY) as stack:
        w = await webhook_world(stack, webhook_sink, path="/hooks/token-abc")
        await deprecate(stack, w["contract"])
        await stack.pump_notices()
        webhook_sink.respond_json(500, {"error": "boom-body-must-not-be-stored"})
        await notifier(stack).run_once()
        row = only_row(sql, stack, w["card"]["id"])
        assert row["last_error"] == "http-500"
        stored = json.dumps(row, default=str)
        assert "boom" not in stored
        assert "token-abc" not in stored
        assert "localhost" not in stored


async def test_a_receiver_that_keeps_failing_ends_the_delivery_but_never_the_channel(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    settings = {**SETTINGS, "notify_retry_schedule_s": [1], "notify_max_attempts": 2}
    async with make_stack(**settings) as stack:
        w = await webhook_world(stack, webhook_sink)
        await stack.subscribe(w["card"]["id"], w["contract"], "3.0.0")
        webhook_sink.answer_always(410)
        await deprecate(stack, w["contract"], "2.3.0")
        await stack.pump_notices()

        assert await notifier(stack).run_once() == 1
        (first,) = deliveries(sql, stack, w["card"]["id"])
        assert first["status"] == "pending"
        assert first["last_error"] == "http-410"
        make_due(sql, stack, first["id"])
        assert await notifier(stack).run_once() == 1
        (failed,) = deliveries(sql, stack, w["card"]["id"])
        assert failed["status"] == "failed"
        assert failed["attempts"] == 2
        assert failed["last_error"] == "http-410"
        # Terminal for that delivery only: nothing more is sent for it.
        assert await notifier(stack).run_once() == 0

        card = await stack.client.get(f"{CARDS}/{w['card']['id']}", headers=bearer(w["manager"]))
        assert card.json()["webhook_enabled"] is True

        # The receiver recovers; the next event is delivered again.
        webhook_sink.answer_always(200)
        assert (await deprecate(stack, w["contract"], "3.0.0")).status_code == 200
        await stack.pump_notices()
        assert await notifier(stack).run_once() == 1
        statuses = sorted(r["status"] for r in deliveries(sql, stack, w["card"]["id"]))
        assert statuses == ["delivered", "failed"]


async def test_a_receiver_that_is_down_never_delays_the_owner(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    async with make_stack(**SETTINGS) as stack:
        up = await webhook_world(stack, webhook_sink)
        started = time.monotonic()
        answered_up = await deprecate(stack, up["contract"])
        assert answered_up.status_code == 200, answered_up.text
        up_elapsed = time.monotonic() - started

        webhook_sink.stop()
        down = await webhook_world(stack, webhook_sink)
        started = time.monotonic()
        answered_down = await deprecate(stack, down["contract"])
        assert time.monotonic() - started < max(2.0, up_elapsed * 10)
        assert answered_down.status_code == 200, answered_down.text
        assert set(answered_down.json()) == set(answered_up.json())
        assert answered_down.json()["semver"] == answered_up.json()["semver"]
        assert answered_down.json()["deprecated"] is True

        await stack.pump_notices()
        # The sink is gone for both cards: each delivery fails to connect and stays pending.
        assert await notifier(stack).run_once() == 2
        for world in (up, down):
            row = only_row(sql, stack, world["card"]["id"])
            assert row["status"] == "pending"
            assert row["attempts"] == 1
            assert row["last_error"] == "connect-error"
            assert row["last_status"] is None


async def test_a_card_disabled_before_the_send_ends_the_delivery_skipped(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    async with make_stack(**SETTINGS) as stack:
        w = await webhook_world(stack, webhook_sink)
        await deprecate(stack, w["contract"])
        await stack.pump_notices()
        off = await stack.client.patch(
            f"{CARDS}/{w['card']['id']}",
            json={"webhook_enabled": False},
            headers=bearer(w["manager"]),
        )
        assert off.status_code == 200, off.text
        assert await notifier(stack).run_once() == 1
        row = only_row(sql, stack, w["card"]["id"])
        assert row["status"] == "skipped"
        assert row["last_error"] == "channel-disabled"
        assert row["attempts"] == 0
        assert webhook_sink.requests == []


async def test_a_missing_secret_or_an_unusable_url_is_a_failed_attempt_with_a_code(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    async with make_stack(**SETTINGS) as stack:
        w = await webhook_world(stack, webhook_sink)
        ids = seed_pending(sql, stack, w, 2)
        sql(stack.contracts_db_url, "DELETE FROM card_webhook_secret")
        assert await notifier(stack).run_once() == 2
        rows = deliveries(sql, stack, w["card"]["id"])
        assert {r["last_error"] for r in rows} == {"no-key"}
        assert {r["status"] for r in rows} == {"pending"}
        assert webhook_sink.requests == []
        assert len(ids) == 2


async def test_a_secret_sealed_with_another_key_is_a_failed_attempt_with_no_key(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    # A rotated or mismatched webhook_secret_key: the stored secret no longer opens, so nothing
    # can be signed and nothing is sent.
    async with make_stack(**SETTINGS) as stack:
        w = await webhook_world(stack, webhook_sink)
        seed_pending(sql, stack, w, 1)
        foreign = Fernet(Fernet.generate_key()).encrypt(b"whsec_other").decode()
        sql(stack.contracts_db_url, "UPDATE card_webhook_secret SET secret_enc = %s", foreign)
        assert await notifier(stack).run_once() == 1
        row = only_row(sql, stack, w["card"]["id"])
        assert (row["status"], row["attempts"], row["last_error"]) == ("pending", 1, "no-key")
        assert webhook_sink.requests == []


async def test_a_url_that_no_longer_validates_is_a_failed_attempt_with_a_code(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    async with make_stack(**SETTINGS) as stack:
        w = await webhook_world(stack, webhook_sink)
        seed_pending(sql, stack, w, 1)
        sql(
            stack.contracts_db_url,
            "UPDATE subscriber_card SET webhook_url = 'ftp://localhost/x' WHERE id = %s",
            w["card"]["id"],
        )
        assert await notifier(stack).run_once() == 1
        row = only_row(sql, stack, w["card"]["id"])
        assert row["last_error"] == "invalid-url"
        assert webhook_sink.requests == []


async def test_two_notifiers_send_each_delivery_exactly_once(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    from dc_contracts.delivery import CardNotifier

    async with make_stack(**SETTINGS, notify_batch_size=2) as stack:
        w = await webhook_world(stack, webhook_sink)
        ids = seed_pending(sql, stack, w, 5)
        state = stack.contracts_app.state
        other = CardNotifier(
            state.session_factory, state.settings, settings_client=state.settings_client
        )
        try:
            handled = 0
            for _ in range(5):
                first, second = await asyncio.gather(notifier(stack).run_once(), other.run_once())
                handled += first + second
                if first + second == 0:
                    break
        finally:
            await other.aclose()
        assert handled == 5
        sent = [r.headers["webhook-id"] for r in webhook_sink.requests]
        assert sorted(sent) == sorted(f"msg_{i}" for i in ids)
        assert all(r["status"] == "delivered" for r in deliveries(sql, stack, w["card"]["id"]))
        assert {r["attempts"] for r in deliveries(sql, stack, w["card"]["id"])} == {1}


async def test_a_claimed_row_whose_worker_crashed_comes_back_after_the_lease(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    async with make_stack(**SETTINGS, notify_lease_s=1) as stack:
        w = await webhook_world(stack, webhook_sink)
        seed_pending(sql, stack, w, 1)
        claimed = await notifier(stack).claim_due()  # leased, then the "worker" dies
        assert len(claimed) == 1
        assert await notifier(stack).run_once() == 0
        assert webhook_sink.requests == []
        await asyncio.sleep(1.2)
        assert await notifier(stack).run_once() == 1
        assert len(webhook_sink.requests) == 1
        assert only_row(sql, stack, w["card"]["id"])["status"] == "delivered"


async def test_both_secrets_sign_during_the_overlap_and_only_the_new_one_after_it(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    async with make_stack(**SETTINGS, webhook_secret_overlap_s=2) as stack:
        w = await webhook_world(stack, webhook_sink)
        old = w["secret"]
        rotated = await stack.client.post(
            f"{CARDS}/{w['card']['id']}/webhook-secret", headers=bearer(w["manager"])
        )
        assert rotated.status_code == 200, rotated.text
        new = rotated.json()["webhook_secret"]
        assert new != old

        await deprecate(stack, w["contract"])
        await stack.pump_notices()
        assert await notifier(stack).run_once() == 1
        (during,) = webhook_sink.requests
        entries = during.headers["webhook-signature"].split(" ")
        assert len(entries) == 2
        assert all(entry.startswith("v1,") for entry in entries)
        assert signature_ok(old, during)
        assert signature_ok(new, during)

        await asyncio.sleep(2.2)
        seed_pending(sql, stack, w, 1)
        assert await notifier(stack).run_once() == 1
        after = webhook_sink.requests[-1]
        assert len(after.headers["webhook-signature"].split(" ")) == 1
        assert signature_ok(new, after)
        assert not signature_ok(old, after)


# --- send-time SSRF re-validation and connection pinning (T-04.1-28, T-04.1-29) -----------------

PUBLIC = "93.184.216.34"
NOT_ALLOW_LISTED: dict[str, Any] = {**SETTINGS, "webhook_allowed_private_hosts": []}


class Resolver:
    """A resolver whose answer the test changes after the URL was accepted."""

    def __init__(self, answer: list[str] | None) -> None:
        self.answer = answer

    async def __call__(self, host: str, port: int) -> list[str]:
        if self.answer is None:
            raise OSError("no such host")
        return list(self.answer)


class Recorder(httpx.AsyncBaseTransport):
    """An outgoing transport that records every request and never connects anywhere."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200)


def record_outgoing(stack: Any) -> Recorder:
    recorder = Recorder()
    notifier(stack)._client = httpx.AsyncClient(transport=recorder)
    return recorder


async def test_an_allow_listed_host_is_delivered_by_name(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    async with make_stack(**SETTINGS) as stack:
        w = await webhook_world(stack, webhook_sink)
        seed_pending(sql, stack, w, 1)
        assert await notifier(stack).run_once() == 1
        (request,) = webhook_sink.requests
        assert request.headers["host"] == f"localhost:{webhook_sink.port}"
        assert only_row(sql, stack, w["card"]["id"])["status"] == "delivered"


async def test_the_request_is_pinned_to_the_validated_address(
    make_stack: MakeStack,
    sql: Sql,
    webhook_sink: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = Resolver([PUBLIC])
    monkeypatch.setattr(webhooks, "_resolve", resolver)
    async with make_stack(**NOT_ALLOW_LISTED) as stack:
        w = await webhook_world(stack, webhook_sink)
        seed_pending(sql, stack, w, 1)
        recorder = record_outgoing(stack)
        assert await notifier(stack).run_once() == 1
        (request,) = recorder.requests
        assert request.url.host == PUBLIC
        assert request.url.port == webhook_sink.port
        assert request.headers["host"] == f"localhost:{webhook_sink.port}"
        assert only_row(sql, stack, w["card"]["id"])["status"] == "delivered"


async def test_a_host_that_now_resolves_to_a_private_address_is_refused_at_send_time(
    make_stack: MakeStack,
    sql: Sql,
    webhook_sink: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = Resolver([PUBLIC])
    monkeypatch.setattr(webhooks, "_resolve", resolver)
    async with make_stack(**NOT_ALLOW_LISTED) as stack:
        w = await webhook_world(stack, webhook_sink)  # accepted: the host was public
        seed_pending(sql, stack, w, 1)
        resolver.answer = ["10.0.0.5"]  # DNS changed after the URL was accepted
        recorder = record_outgoing(stack)
        assert await notifier(stack).run_once() == 1
        row = only_row(sql, stack, w["card"]["id"])
        assert row["last_error"] == "ssrf-blocked"
        assert row["status"] == "pending"  # an ordinary failed attempt, retried on the schedule
        assert row["attempts"] == 1
        assert recorder.requests == []
        assert webhook_sink.requests == []


async def test_a_mixed_answer_with_one_private_address_is_refused(
    make_stack: MakeStack,
    sql: Sql,
    webhook_sink: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = Resolver([PUBLIC])
    monkeypatch.setattr(webhooks, "_resolve", resolver)
    async with make_stack(**NOT_ALLOW_LISTED) as stack:
        w = await webhook_world(stack, webhook_sink)
        seed_pending(sql, stack, w, 1)
        resolver.answer = [PUBLIC, "::ffff:10.0.0.1"]
        recorder = record_outgoing(stack)
        assert await notifier(stack).run_once() == 1
        assert only_row(sql, stack, w["card"]["id"])["last_error"] == "ssrf-blocked"
        assert recorder.requests == []


async def test_a_host_that_no_longer_resolves_is_a_dns_error_with_no_request(
    make_stack: MakeStack,
    sql: Sql,
    webhook_sink: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = Resolver([PUBLIC])
    monkeypatch.setattr(webhooks, "_resolve", resolver)
    async with make_stack(**NOT_ALLOW_LISTED) as stack:
        w = await webhook_world(stack, webhook_sink)
        seed_pending(sql, stack, w, 1)
        resolver.answer = None
        recorder = record_outgoing(stack)
        assert await notifier(stack).run_once() == 1
        row = only_row(sql, stack, w["card"]["id"])
        assert row["last_error"] == "dns-error"
        assert recorder.requests == []


class V6Unreachable(Recorder):
    """No IPv6 egress: connecting to an IPv6 literal fails before anything is sent."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if ":" in request.url.host:
            raise httpx.ConnectError("network is unreachable")
        return httpx.Response(200)


async def test_an_unreachable_first_address_falls_through_to_the_next_validated_one(
    make_stack: MakeStack,
    sql: Sql,
    webhook_sink: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WR-01: a dual-stack receiver whose AAAA address comes first must still be delivered to.
    v6 = "2606:2800:220:1:248:1893:25c8:1946"
    resolver = Resolver([PUBLIC])
    monkeypatch.setattr(webhooks, "_resolve", resolver)
    async with make_stack(**NOT_ALLOW_LISTED) as stack:
        w = await webhook_world(stack, webhook_sink)
        seed_pending(sql, stack, w, 1)
        resolver.answer = [v6, PUBLIC]
        recorder = V6Unreachable()
        notifier(stack)._client = httpx.AsyncClient(transport=recorder)
        assert await notifier(stack).run_once() == 1
        assert [r.url.host for r in recorder.requests] == [v6, PUBLIC]
        row = only_row(sql, stack, w["card"]["id"])
        assert row["status"] == "delivered"
        assert row["attempts"] == 1


async def test_a_receiver_answer_is_final_and_no_other_address_is_tried(
    make_stack: MakeStack,
    sql: Sql,
    webhook_sink: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = Resolver([PUBLIC])
    monkeypatch.setattr(webhooks, "_resolve", resolver)
    async with make_stack(**NOT_ALLOW_LISTED) as stack:
        w = await webhook_world(stack, webhook_sink)
        seed_pending(sql, stack, w, 1)
        resolver.answer = [PUBLIC, "8.8.8.8"]

        class Refuses(Recorder):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                self.requests.append(request)
                return httpx.Response(500)

        recorder = Refuses()
        notifier(stack)._client = httpx.AsyncClient(transport=recorder)
        assert await notifier(stack).run_once() == 1
        assert len(recorder.requests) == 1
        assert only_row(sql, stack, w["card"]["id"])["last_error"] == "http-500"


async def test_a_deterministic_send_error_counts_as_an_attempt_and_ends_the_delivery(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WR-03: an exception in the send step must not leave the row leased and uncounted for ever.
    settings = {**SETTINGS, "notify_retry_schedule_s": [1], "notify_max_attempts": 2}
    async with make_stack(**settings) as stack:
        w = await webhook_world(stack, webhook_sink)
        (delivery_id,) = seed_pending(sql, stack, w, 1)

        async def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise ValueError("deterministic")

        monkeypatch.setattr(notifier(stack), "_send_webhook", boom)
        assert await notifier(stack).run_once() == 1
        row = only_row(sql, stack, w["card"]["id"])
        assert (row["status"], row["attempts"], row["last_error"]) == (
            "pending",
            1,
            "internal-error",
        )
        assert wait_seconds(sql, stack, delivery_id) <= 2  # the retry schedule, not the lease

        make_due(sql, stack, delivery_id)
        assert await notifier(stack).run_once() == 1
        row = only_row(sql, stack, w["card"]["id"])
        assert (row["status"], row["attempts"]) == ("failed", 2)
        assert webhook_sink.requests == []


async def test_a_card_archived_after_the_notice_is_not_delivered_to(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    # WR-04: archiving stops queued and retrying deliveries as well as new notices.
    async with make_stack(**SETTINGS) as stack:
        w = await webhook_world(stack, webhook_sink)
        await deprecate(stack, w["contract"])
        await stack.pump_notices()
        archived = await stack.client.post(
            f"{CARDS}/{w['card']['id']}/archive", headers=bearer(w["manager"])
        )
        assert archived.status_code == 200, archived.text
        assert await notifier(stack).run_once() == 1
        row = only_row(sql, stack, w["card"]["id"])
        assert row["status"] == "skipped"
        assert row["last_error"] == "card-archived"
        assert row["attempts"] == 0
        assert webhook_sink.requests == []
