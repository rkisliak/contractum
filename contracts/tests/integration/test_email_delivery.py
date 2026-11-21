"""SUB-07 / D-98 / D-99 / D-105: the email channel of the delivery worker.

A card set to email gets each recorded notice handed to the mail service (one POST to
``/internal/v1/mail/send`` with the notifier key, addressed to every email contact). The mail
service is stood in for by the local HTTP sink; the real contracts -> mail -> Mailpit chain is
proved by the Plan 17 end-to-end journey. Tests drive ``app.state.card_notifier.run_once()`` by
hand, the background loop is off.
"""

from __future__ import annotations

import asyncio
import io
import json
import secrets
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import structlog
from cryptography.fernet import Fernet
from dc_contracts import webhooks
from dc_core.logging import configure_logging
from dc_core.mail import MAIL_SEND_PATH, MailSendIn
from pydantic import SecretStr

pytestmark = pytest.mark.integration

CARDS = "/api/v1/contracts/cards"

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

CONTACTS: list[dict[str, str]] = [
    {"kind": "email", "value": "a@example.test"},
    {"kind": "email", "value": "b@example.test"},
    {"kind": "url", "value": "https://wiki.example.test/team"},
    {"kind": "text", "value": "ask in the data channel"},
]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def new_key() -> str:
    """A notifier-shaped key generated per test: no token literal lives in a tracked file."""
    return f"dcp_{secrets.token_hex(8)}_{secrets.token_urlsafe(32)}"


@pytest.fixture
def mail_sink(webhook_sink: Any) -> Iterator[Any]:
    """A second sink (same class as ``webhook_sink``) standing in for the mail service."""
    sink = type(webhook_sink)()
    try:
        yield sink
    finally:
        sink.stop()


def mail_settings(sink: Any, key: str, **extra: Any) -> dict[str, Any]:
    return {
        "mail_url": f"http://localhost:{sink.port}",
        "mail_api_key": SecretStr(key),
        **extra,
    }


def today_plus(days: int) -> Any:
    return (datetime.now(UTC) + timedelta(days=days)).date()


async def email_world(
    stack: Any,
    *,
    contacts: list[dict[str, str]] | None = None,
    email_enabled: bool = True,
) -> dict[str, Any]:
    """Contract 2.3.0 (Latest) and 3.0.0; an email card pins 2.3.0."""
    chain = await stack.chain()
    contract, versions = await stack.publish_versions(chain.team, ["2.3.0", "3.0.0"])
    token, principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal, "consumer", chain.team)
    card = await stack.create_card(
        chain.team,
        token=token,
        name="orders consumers",
        contacts=CONTACTS if contacts is None else contacts,
        email_enabled=email_enabled,
    )
    pinned = await stack.subscribe(card["id"], contract, "2.3.0")
    assert pinned.status_code == 201, pinned.text
    return {"chain": chain, "contract": contract, "card": card, "manager": token}


async def deprecate(stack: Any, contract: str, ref: str = "2.3.0") -> httpx.Response:
    return await stack.client.post(
        f"/api/v1/contracts/contracts/{contract}/versions/{ref}/deprecate",
        json={"sunset_date": today_plus(60).isoformat()},
        headers=bearer(stack.admin_token),
    )


def deliveries(sql: Sql, stack: Any, card_id: str, channel: str = "email") -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT d.* FROM card_notice_delivery d JOIN card_notice n ON n.id = d.notice_id "
        "WHERE n.card_id = %s AND d.channel = %s ORDER BY d.id",
        card_id,
        channel,
    )


def notifier(stack: Any) -> Any:
    return stack.contracts_app.state.card_notifier


# --- tracer: an email card's deprecation is handed to the mail service -------------------------


async def test_an_email_card_gets_a_deprecation_handed_to_the_mail_service(
    make_stack: MakeStack, sql: Sql, mail_sink: Any
) -> None:
    key = new_key()
    mail_sink.respond_json(200, {"accepted": 2, "refused": 0})
    async with make_stack(**mail_settings(mail_sink, key)) as stack:
        w = await email_world(stack)
        response = await deprecate(stack, w["contract"])
        assert response.status_code == 200, response.text
        assert await stack.pump_notices() == 1

        (pending,) = deliveries(sql, stack, w["card"]["id"])
        assert pending["status"] == "pending"
        assert await notifier(stack).run_once() == 1

        (request,) = mail_sink.requests
        assert request.method == "POST"
        assert request.path == MAIL_SEND_PATH == "/internal/v1/mail/send"
        assert request.headers["authorization"] == "Bearer " + key
        assert request.headers["content-type"] == "application/json"
        message = MailSendIn.model_validate_json(request.body)
        assert message.to == ["a@example.test", "b@example.test"]
        assert "deprecated" in message.subject
        assert "orders consumers" in message.text
        assert str(pending["notice_id"]) in message.text

        (done,) = deliveries(sql, stack, w["card"]["id"])
        assert done["status"] == "delivered"
        assert done["attempts"] == 1
        assert done["last_error"] is None
        assert done["delivered_at"] is not None


async def test_a_card_with_both_channels_gets_one_delivery_per_channel(
    make_stack: MakeStack,
    sql: Sql,
    mail_sink: Any,
    webhook_sink: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake(host: str, port: int) -> list[str]:
        return ["127.0.0.1"]

    monkeypatch.setattr(webhooks, "_resolve", fake)
    key = new_key()
    mail_sink.respond_json(200, {"accepted": 2, "refused": 0})
    settings = mail_settings(
        mail_sink,
        key,
        webhook_secret_key=Fernet.generate_key().decode(),
        webhook_require_https=False,
        webhook_allowed_private_hosts=["localhost"],
    )
    async with make_stack(**settings) as stack:
        w = await email_world(stack)
        enabled = await stack.client.patch(
            f"{CARDS}/{w['card']['id']}",
            json={"webhook_url": webhook_sink.url("/hooks"), "webhook_enabled": True},
            headers=bearer(w["manager"]),
        )
        assert enabled.status_code == 200, enabled.text
        assert (await deprecate(stack, w["contract"])).status_code == 200
        assert await stack.pump_notices() == 1

        (email,) = deliveries(sql, stack, w["card"]["id"], "email")
        (hook,) = deliveries(sql, stack, w["card"]["id"], "webhook")
        assert email["notice_id"] == hook["notice_id"]
        assert await notifier(stack).run_once() == 2

        (email,) = deliveries(sql, stack, w["card"]["id"], "email")
        (hook,) = deliveries(sql, stack, w["card"]["id"], "webhook")
        assert email["status"] == "delivered"
        assert hook["status"] == "delivered"
        assert len(mail_sink.requests) == 1
        assert len(webhook_sink.requests) == 1
        assert json.loads(webhook_sink.requests[0].body)["type"] == "contract.version.deprecated"


# --- refusals, mail-service failures and missing configuration ---------------------------------

FAST_RETRY: dict[str, Any] = {"notify_retry_schedule_s": [1, 1, 1], "notify_max_attempts": 4}


def only_row(sql: Sql, stack: Any, card_id: str) -> dict[str, Any]:
    (row,) = deliveries(sql, stack, card_id)
    return row


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


async def one_pass(stack: Any, contract: str) -> None:
    assert (await deprecate(stack, contract)).status_code == 200
    assert await stack.pump_notices() == 1
    assert await notifier(stack).run_once() == 1


async def test_a_partial_refusal_is_delivered_and_never_resent(
    make_stack: MakeStack, sql: Sql, mail_sink: Any
) -> None:
    mail_sink.respond_json(200, {"accepted": 1, "refused": 1})
    async with make_stack(**mail_settings(mail_sink, new_key())) as stack:
        w = await email_world(stack)
        await one_pass(stack, w["contract"])
        row = only_row(sql, stack, w["card"]["id"])
        assert row["status"] == "delivered"
        assert row["attempts"] == 1
        assert row["last_error"] == "mail-recipients-refused:1"
        assert await notifier(stack).run_once() == 0
        assert len(mail_sink.requests) == 1


async def test_every_recipient_refused_is_a_failed_attempt_on_the_schedule(
    make_stack: MakeStack, sql: Sql, mail_sink: Any
) -> None:
    mail_sink.respond_json(200, {"accepted": 0, "refused": 2})
    async with make_stack(**mail_settings(mail_sink, new_key(), **FAST_RETRY)) as stack:
        w = await email_world(stack)
        await one_pass(stack, w["contract"])
        row = only_row(sql, stack, w["card"]["id"])
        assert row["status"] == "pending"
        assert row["attempts"] == 1
        assert row["last_error"] == "mail-recipients-refused"
        assert 0.5 <= wait_seconds(sql, stack, row["id"]) <= 1.3
        assert await notifier(stack).run_once() == 0


async def test_a_temporarily_refused_recipient_makes_the_attempt_a_retry(
    make_stack: MakeStack, sql: Sql, mail_sink: Any
) -> None:
    # WR-06: a 4xx refusal (greylisting) is not delivered; it is retried on the schedule.
    mail_sink.respond_json(200, {"accepted": 1, "refused": 1, "deferred": 1})
    async with make_stack(**mail_settings(mail_sink, new_key(), **FAST_RETRY)) as stack:
        w = await email_world(stack)
        await one_pass(stack, w["contract"])
        row = only_row(sql, stack, w["card"]["id"])
        assert row["status"] == "pending"
        assert row["attempts"] == 1
        assert row["last_error"] == "mail-recipients-deferred"
        assert 0.5 <= wait_seconds(sql, stack, row["id"]) <= 1.3


@pytest.mark.parametrize(("status", "code"), [(503, "mail-http-503"), (401, "mail-http-401")])
async def test_a_mail_service_error_status_is_a_failed_attempt(
    make_stack: MakeStack, sql: Sql, mail_sink: Any, status: int, code: str
) -> None:
    mail_sink.respond_json(status, {"detail": "boom-body-must-not-be-stored"})
    async with make_stack(**mail_settings(mail_sink, new_key(), **FAST_RETRY)) as stack:
        w = await email_world(stack)
        await one_pass(stack, w["contract"])
        row = only_row(sql, stack, w["card"]["id"])
        assert row["status"] == "pending"
        assert row["last_error"] == code
        assert row["last_status"] == status
        assert "boom" not in json.dumps(row, default=str)


async def test_an_unreadable_answer_is_a_failed_attempt(
    make_stack: MakeStack, sql: Sql, mail_sink: Any
) -> None:
    # The sink answers 200 with an empty body when nothing is queued.
    async with make_stack(**mail_settings(mail_sink, new_key(), **FAST_RETRY)) as stack:
        w = await email_world(stack)
        await one_pass(stack, w["contract"])
        row = only_row(sql, stack, w["card"]["id"])
        assert row["status"] == "pending"
        assert row["last_error"] == "mail-bad-response"


async def test_a_mail_service_that_is_down_never_blocks_the_owner(
    make_stack: MakeStack, sql: Sql, mail_sink: Any
) -> None:
    async with make_stack(**mail_settings(mail_sink, new_key(), **FAST_RETRY)) as stack:
        w = await email_world(stack)
        mail_sink.stop()
        started = time.monotonic()
        response = await deprecate(stack, w["contract"])
        assert response.status_code == 200, response.text
        assert time.monotonic() - started < 2.0
        assert await stack.pump_notices() == 1
        assert await notifier(stack).run_once() == 1
        row = only_row(sql, stack, w["card"]["id"])
        assert row["status"] == "pending"
        assert row["last_error"] == "mail-connect-error"


async def test_a_missing_mail_configuration_fails_until_the_cap_but_keeps_the_channel(
    make_stack: MakeStack, sql: Sql
) -> None:
    settings = {"notify_retry_schedule_s": [1], "notify_max_attempts": 2}
    async with make_stack(**settings) as stack:
        w = await email_world(stack)
        card_id = w["card"]["id"]
        assert (await stack.subscribe(card_id, w["contract"], "3.0.0")).status_code == 201
        await one_pass(stack, w["contract"])
        first = only_row(sql, stack, card_id)
        assert first["status"] == "pending"
        assert first["last_error"] == "mail-not-configured"
        make_due(sql, stack, first["id"])
        assert await notifier(stack).run_once() == 1
        failed = only_row(sql, stack, card_id)
        assert failed["status"] == "failed"
        assert failed["attempts"] == 2
        make_due(sql, stack, failed["id"])
        assert await notifier(stack).run_once() == 0

        card = sql(
            stack.contracts_db_url,
            "SELECT email_enabled FROM subscriber_card WHERE id = %s",
            card_id,
        )[0]
        assert card["email_enabled"] is True

        # The next notice is a new delivery of its own.
        assert (await deprecate(stack, w["contract"], "3.0.0")).status_code == 200
        assert await stack.pump_notices() == 1
        rows = deliveries(sql, stack, card_id)
        assert [r["status"] for r in rows] == ["failed", "pending"]


async def test_a_card_without_email_contacts_skips_the_delivery(
    make_stack: MakeStack, sql: Sql, mail_sink: Any
) -> None:
    contacts = [{"kind": "text", "value": "ask in the data channel"}]
    async with make_stack(**mail_settings(mail_sink, new_key())) as stack:
        w = await email_world(stack, contacts=contacts)
        await one_pass(stack, w["contract"])
        row = only_row(sql, stack, w["card"]["id"])
        assert row["status"] == "skipped"
        assert row["last_error"] == "no-recipients"
        assert mail_sink.requests == []


async def test_a_channel_turned_off_after_the_notice_skips_the_delivery(
    make_stack: MakeStack, sql: Sql, mail_sink: Any
) -> None:
    async with make_stack(**mail_settings(mail_sink, new_key())) as stack:
        w = await email_world(stack)
        assert (await deprecate(stack, w["contract"])).status_code == 200
        assert await stack.pump_notices() == 1
        off = await stack.client.patch(
            f"{CARDS}/{w['card']['id']}",
            json={"email_enabled": False},
            headers=bearer(w["manager"]),
        )
        assert off.status_code == 200, off.text
        assert await notifier(stack).run_once() == 1
        row = only_row(sql, stack, w["card"]["id"])
        assert row["status"] == "skipped"
        assert row["last_error"] == "channel-disabled"
        assert mail_sink.requests == []


async def test_contacts_are_read_at_send_time(
    make_stack: MakeStack, sql: Sql, mail_sink: Any
) -> None:
    mail_sink.respond_json(200, {"accepted": 1, "refused": 0})
    async with make_stack(**mail_settings(mail_sink, new_key())) as stack:
        w = await email_world(stack)
        assert (await deprecate(stack, w["contract"])).status_code == 200
        assert await stack.pump_notices() == 1
        changed = await stack.client.patch(
            f"{CARDS}/{w['card']['id']}",
            json={"contacts": [{"kind": "email", "value": "late@example.test"}]},
            headers=bearer(w["manager"]),
        )
        assert changed.status_code == 200, changed.text
        assert await notifier(stack).run_once() == 1
        (request,) = mail_sink.requests
        assert MailSendIn.model_validate_json(request.body).to == ["late@example.test"]


class LogCapture(io.StringIO):
    """dc_core's structlog JSON output at DEBUG so nothing is filtered out."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def write(self, s: str) -> int:
        self.lines.extend(line for line in s.splitlines() if line)
        return super().write(s)


async def test_neither_the_key_nor_an_address_is_in_any_log_line_or_delivery_row(
    make_stack: MakeStack, sql: Sql, mail_sink: Any
) -> None:
    key = new_key()
    logs = LogCapture()
    mail_sink.respond_json(503, {"detail": "down"})
    mail_sink.respond_json(200, {"accepted": 1, "refused": 1})
    try:
        async with make_stack(**mail_settings(mail_sink, key, **FAST_RETRY)) as stack:
            configure_logging("contracts-mail-scan", "DEBUG", stream=logs)
            w = await email_world(stack)
            await one_pass(stack, w["contract"])
            row = only_row(sql, stack, w["card"]["id"])
            await asyncio.sleep(1.1)
            make_due(sql, stack, row["id"])
            assert await notifier(stack).run_once() == 1

            rows = sql(stack.contracts_db_url, "SELECT * FROM card_notice_delivery")
            notices = sql(stack.contracts_db_url, "SELECT * FROM card_notice")
            stores = {
                "delivery rows": json.dumps(rows, default=str),
                "notice rows": json.dumps(notices, default=str),
                "log lines": "\n".join(logs.lines),
            }
            assert any("card_notice_delivery" in line for line in logs.lines), (
                "no delivery log line was captured: the scan would prove nothing"
            )
            needles = {
                "the mail key": key,
                "the key secret part": key.split("_", 2)[2],
                "address a": "a@example.test",
                "address b": "b@example.test",
            }
            hits = [
                f"{store} contains {label}"
                for store, text in stores.items()
                for label, needle in needles.items()
                if needle in text
            ]
            assert hits == []
    finally:
        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()
