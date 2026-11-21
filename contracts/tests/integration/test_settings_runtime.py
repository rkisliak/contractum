"""Notification settings at runtime (quick 261120-0yd): the delivery worker, the sunset scheduler,
the card webhook PATCH and the Settings test email.

The stored section is served to the worker, the scheduler and the PATCH route by a stand-in for
identity's runtime route (``stored_client``), so each test controls exactly what is "stored". The
worker and the scheduler read it through ``<component>.settings_client``; the PATCH route reads
``app.state.settings_client``. Tests drive ``card_notifier.run_once()`` and
``sunset_scheduler.scan_once()`` by hand.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from dc_contracts import webhooks
from dc_core.platform_settings import RuntimeSettingsClient, normalise_section
from pydantic import SecretStr

pytestmark = pytest.mark.integration

CARDS = "/api/v1/contracts/cards"
TEST_EMAIL = "/api/v1/contracts/settings/test-email"
KEY = Fernet.generate_key().decode()
ADDRESS = "settings-test@example.com"

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

PUBLIC = {
    "hooks.example.org": ["93.184.216.34"],
    "hooks.example.net": ["93.184.216.35"],
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def new_key() -> str:
    return f"dcp_{secrets.token_hex(8)}_{secrets.token_urlsafe(32)}"


@pytest.fixture(autouse=True)
def resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(host: str, port: int) -> list[str]:
        if host == "localhost":
            return ["127.0.0.1"]
        if host in PUBLIC:
            return list(PUBLIC[host])
        raise OSError("no such host")

    monkeypatch.setattr(webhooks, "_resolve", fake)


def stored_client(value: dict[str, Any] | None, *, down: bool = False) -> RuntimeSettingsClient:
    """A runtime client whose "identity" serves ``value`` as the notifications section (after the
    section model normalised it), or is unreachable."""
    section = normalise_section("notifications", value or {})

    def handler(request: httpx.Request) -> httpx.Response:
        if down:
            raise httpx.ConnectError("identity is unreachable", request=request)
        return httpx.Response(200, json={"sections": {"notifications": section}})

    return RuntimeSettingsClient(
        "http://identity:8000", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def notifier(stack: Any) -> Any:
    return stack.contracts_app.state.card_notifier


def scheduler(stack: Any) -> Any:
    return stack.contracts_app.state.sunset_scheduler


def today_plus(days: int) -> date:
    return (datetime.now(UTC) + timedelta(days=days)).date()


async def webhook_world(stack: Any, url: str) -> dict[str, Any]:
    """Contract 2.3.0 (Latest) and 3.0.0; a webhook card (URL ``url``) pins 2.3.0."""
    chain = await stack.chain()
    contract, versions = await stack.publish_versions(chain.team, ["2.3.0", "3.0.0"])
    token, principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal, "consumer", chain.team)
    card = await stack.create_card(chain.team, token=token, name="orders consumers")
    enabled = await stack.client.patch(
        f"{CARDS}/{card['id']}",
        json={"webhook_url": url, "webhook_enabled": True},
        headers=bearer(token),
    )
    assert enabled.status_code == 200, enabled.text
    pinned = await stack.subscribe(card["id"], contract, "2.3.0")
    assert pinned.status_code == 201, pinned.text
    return {"contract": contract, "card": card, "manager": token, "versions": versions}


async def deprecate(stack: Any, contract: str, days: int = 60) -> None:
    response = await stack.client.post(
        f"/api/v1/contracts/contracts/{contract}/versions/2.3.0/deprecate",
        json={"sunset_date": today_plus(days).isoformat()},
        headers=bearer(stack.admin_token),
    )
    assert response.status_code == 200, response.text


def deliveries(
    sql: Sql, stack: Any, card_id: str, channel: str = "webhook"
) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT d.* FROM card_notice_delivery d JOIN card_notice n ON n.id = d.notice_id "
        "WHERE n.card_id = %s AND d.channel = %s ORDER BY d.id",
        card_id,
        channel,
    )


def make_due(sql: Sql, stack: Any, delivery_id: Any) -> None:
    sql(
        stack.contracts_db_url,
        "UPDATE card_notice_delivery SET next_attempt_at = now() WHERE id = %s",
        delivery_id,
    )


def wait_seconds(sql: Sql, stack: Any, delivery_id: Any) -> float:
    row = sql(
        stack.contracts_db_url,
        "SELECT extract(epoch FROM next_attempt_at - now()) AS wait "
        "FROM card_notice_delivery WHERE id = %s",
        delivery_id,
    )[0]
    return float(row["wait"])


WORKER = {
    "webhook_secret_key": KEY,
    "webhook_require_https": False,
    "webhook_allowed_private_hosts": ["localhost"],
}


def worker_settings(**extra: Any) -> dict[str, Any]:
    return {**WORKER, "mail_api_key": SecretStr(new_key()), **extra}


# --- N4: retry schedule and attempt cap ----------------------------------------------------------


async def test_the_worker_uses_the_stored_retry_schedule_and_max_attempts(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    webhook_sink.answer_always(500)
    async with make_stack(**worker_settings()) as stack:
        w = await webhook_world(stack, webhook_sink.url("/hooks"))
        notifier(stack).settings_client = stored_client(
            {
                "retry_schedule_s": [100, 200],
                "max_attempts": 2,
                "webhook_allowed_hosts": ["localhost"],
            }
        )
        await deprecate(stack, w["contract"])
        assert await stack.pump_notices() == 1
        assert await notifier(stack).run_once() == 1

        (row,) = deliveries(sql, stack, w["card"]["id"])
        assert row["status"] == "pending"
        assert row["attempts"] == 1
        assert 85 <= wait_seconds(sql, stack, row["id"]) <= 115  # 100 s +-10 %

        make_due(sql, stack, row["id"])
        assert await notifier(stack).run_once() == 1
        (row,) = deliveries(sql, stack, w["card"]["id"])
        assert row["status"] == "failed"  # the stored cap is 2, the env cap is 7
        assert row["attempts"] == 2
        assert len(webhook_sink.requests) == 2


# --- N6: the allowed-hosts list ------------------------------------------------------------------


async def test_a_host_outside_the_list_ends_the_delivery_failed_at_once_and_sends_nothing(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    async with make_stack(**worker_settings()) as stack:
        w = await webhook_world(stack, webhook_sink.url("/hooks"))
        notifier(stack).settings_client = stored_client(
            {"webhook_allowed_hosts": ["*.example.org"]}
        )
        await deprecate(stack, w["contract"])
        assert await stack.pump_notices() == 1
        assert await notifier(stack).run_once() == 1
        (row,) = deliveries(sql, stack, w["card"]["id"])
        assert row["status"] == "failed"
        assert row["last_error"] == "webhook-host-not-allowed"
        assert row["attempts"] == 1
        assert webhook_sink.requests == []  # no connection, no retry


async def test_an_empty_list_allows_no_host_and_sends_nothing(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    """Quick 261120-s0p (item 6): the list is mandatory, so an empty one is not "no restriction"."""
    async with make_stack(**worker_settings()) as stack:
        w = await webhook_world(stack, webhook_sink.url("/hooks"))
        notifier(stack).settings_client = stored_client({})
        await deprecate(stack, w["contract"])
        assert await stack.pump_notices() == 1
        assert await notifier(stack).run_once() == 1
        (row,) = deliveries(sql, stack, w["card"]["id"])
        assert row["status"] == "failed"
        assert row["last_error"] == "webhook-host-not-allowed"
        assert row["attempts"] == 1
        assert webhook_sink.requests == []


async def test_a_worker_without_a_notifier_key_allows_no_webhook_host(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    """With no ``mail_api_key`` the worker cannot read the list, so it allows no host."""
    async with make_stack(**WORKER) as stack:
        w = await webhook_world(stack, webhook_sink.url("/hooks"))
        await deprecate(stack, w["contract"])
        assert await stack.pump_notices() == 1
        assert await notifier(stack).run_once() == 1
        (row,) = deliveries(sql, stack, w["card"]["id"])
        assert row["status"] == "failed"
        assert row["last_error"] == "webhook-host-not-allowed"
        assert webhook_sink.requests == []


async def test_a_matching_host_is_still_checked_for_ssrf(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    """The list only narrows: ``localhost`` is on the Helm private allow-list, so it is sent to,
    and listing it adds nothing for any other private name."""
    async with make_stack(**worker_settings()) as stack:
        w = await webhook_world(stack, webhook_sink.url("/hooks"))
        notifier(stack).settings_client = stored_client({"webhook_allowed_hosts": ["localhost"]})
        await deprecate(stack, w["contract"])
        assert await stack.pump_notices() == 1
        assert await notifier(stack).run_once() == 1
        (row,) = deliveries(sql, stack, w["card"]["id"])
        assert row["status"] == "delivered"
        assert len(webhook_sink.requests) == 1


async def test_an_unreadable_list_defers_the_delivery_without_using_an_attempt(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    async with make_stack(**worker_settings()) as stack:
        w = await webhook_world(stack, webhook_sink.url("/hooks"))
        notifier(stack).settings_client = stored_client(None, down=True)
        await deprecate(stack, w["contract"])
        assert await stack.pump_notices() == 1
        assert await notifier(stack).run_once() == 1
        (row,) = deliveries(sql, stack, w["card"]["id"])
        assert row["status"] == "pending"
        assert row["attempts"] == 0
        assert row["last_error"] == "settings-unavailable"
        assert 45 <= wait_seconds(sql, stack, row["id"]) <= 61
        assert webhook_sink.requests == []

        # identity is back: the next pass delivers it and counts the first attempt
        notifier(stack).settings_client = stored_client({"webhook_allowed_hosts": ["localhost"]})
        make_due(sql, stack, row["id"])
        assert await notifier(stack).run_once() == 1
        (row,) = deliveries(sql, stack, w["card"]["id"])
        assert row["status"] == "delivered"
        assert row["attempts"] == 1


async def test_patching_a_card_checks_the_list_after_the_url_rules(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        chain = await stack.chain()
        token, principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
        stack.grant_sql(principal, "consumer", chain.team)
        card = await stack.create_card(chain.team, token=token)
        url = f"{CARDS}/{card['id']}"
        stack.contracts_app.state.settings_client = stored_client(
            {"webhook_allowed_hosts": ["*.example.org"]}
        )
        refused = await stack.client.patch(
            url, json={"webhook_url": "https://hooks.example.net/x"}, headers=bearer(token)
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["type"] == "urn:dc:problem:webhook-host-not-allowed"
        assert "example.net" not in refused.text
        # An http URL is still refused by the existing rule first (invalid-webhook-url).
        plain = await stack.client.patch(
            url, json={"webhook_url": "http://hooks.example.org/x"}, headers=bearer(token)
        )
        assert plain.status_code == 422
        assert plain.json()["type"] == "urn:dc:problem:invalid-webhook-url"
        ok = await stack.client.patch(
            url, json={"webhook_url": "https://hooks.example.org/x"}, headers=bearer(token)
        )
        assert ok.status_code == 200, ok.text
        # The list never admits a private address.
        stack.contracts_app.state.settings_client = stored_client(
            {"webhook_allowed_hosts": ["localhost"]}
        )
        private = await stack.client.patch(
            url, json={"webhook_url": "https://localhost/x"}, headers=bearer(token)
        )
        assert private.status_code == 422
        assert private.json()["type"] == "urn:dc:problem:invalid-webhook-url"


async def test_patching_a_card_url_is_503_when_the_list_was_never_read(
    make_stack: MakeStack,
) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        chain = await stack.chain()
        token, principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
        stack.grant_sql(principal, "consumer", chain.team)
        card = await stack.create_card(chain.team, token=token)
        stack.contracts_app.state.settings_client = stored_client(None, down=True)
        down = await stack.client.patch(
            f"{CARDS}/{card['id']}",
            json={"webhook_url": "https://hooks.example.org/x"},
            headers=bearer(token),
        )
        assert down.status_code == 503, down.text
        assert down.json()["type"] == "urn:dc:problem:settings-unavailable"
        # A PATCH that does not touch the URL does not need the list.
        rename = await stack.client.patch(
            f"{CARDS}/{card['id']}", json={"name": "renamed"}, headers=bearer(token)
        )
        assert rename.status_code == 200, rename.text


async def test_patching_a_card_to_an_unlisted_host_names_where_to_add_it(
    make_stack: MakeStack,
) -> None:
    """An empty list is a refusal too, and the detail points at Settings > Notifications."""
    async with make_stack(webhook_secret_key=KEY, webhook_hosts=None) as stack:
        chain = await stack.chain()
        token, principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
        stack.grant_sql(principal, "consumer", chain.team)
        card = await stack.create_card(chain.team, token=token)
        refused = await stack.client.patch(
            f"{CARDS}/{card['id']}",
            json={"webhook_url": "https://hooks.example.org/x"},
            headers=bearer(token),
        )
        assert refused.status_code == 422, refused.text
        body = refused.json()
        assert body["type"] == "urn:dc:problem:webhook-host-not-allowed"
        assert "Settings > Notifications" in body["detail"]
        assert "example.org" not in refused.text


async def test_a_host_the_admin_just_added_is_accepted_at_once(make_stack: MakeStack) -> None:
    """A URL edit asks identity again, so the 30 s cache never refuses a freshly listed host."""
    async with make_stack(webhook_secret_key=KEY, webhook_hosts=None) as stack:
        chain = await stack.chain()
        token, principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
        stack.grant_sql(principal, "consumer", chain.team)
        card = await stack.create_card(chain.team, token=token)
        url = f"{CARDS}/{card['id']}"
        body = {"webhook_url": "https://hooks.example.org/x"}
        refused = await stack.client.patch(url, json=body, headers=bearer(token))
        assert refused.status_code == 422, refused.text
        saved = await stack.identity.put(
            "/api/v1/identity/settings/notifications",
            json={"webhook_allowed_hosts": ["hooks.example.org"]},
            headers={**bearer(stack.admin_token), "If-Match": '"notifications-r0"'},
        )
        assert saved.status_code == 200, saved.text
        accepted = await stack.client.patch(url, json=body, headers=bearer(token))
        assert accepted.status_code == 200, accepted.text


async def test_until_the_seed_is_confirmed_a_url_edit_is_503_and_deliveries_wait(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    async with make_stack(**worker_settings()) as stack:
        w = await webhook_world(stack, webhook_sink.url("/hooks"))
        notifier(stack).settings_client = stored_client({"webhook_allowed_hosts": ["localhost"]})
        seed = stack.contracts_app.state.webhook_host_seed
        seed.done = False
        edit = await stack.client.patch(
            f"{CARDS}/{w['card']['id']}",
            json={"webhook_url": "https://hooks.example.test/x"},
            headers=bearer(w["manager"]),
        )
        assert edit.status_code == 503, edit.text
        assert edit.json()["type"] == "urn:dc:problem:settings-unavailable"
        await deprecate(stack, w["contract"])
        assert await stack.pump_notices() == 1
        assert await notifier(stack).run_once() == 1
        (row,) = deliveries(sql, stack, w["card"]["id"])
        assert row["status"] == "pending"
        assert row["attempts"] == 0
        assert row["last_error"] == "settings-unavailable"
        assert webhook_sink.requests == []
        seed.done = True
        make_due(sql, stack, row["id"])
        assert await notifier(stack).run_once() == 1
        (row,) = deliveries(sql, stack, w["card"]["id"])
        assert row["status"] == "delivered"


# --- N5: sunset lead times -----------------------------------------------------------------------


async def test_the_scheduler_uses_the_stored_leads_on_every_scan(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(**worker_settings(), sunset_lead_days=(30, 7)) as stack:
        chain = await stack.chain()
        contract, _ = await stack.publish_versions(chain.team, ["2.3.0", "3.0.0"])
        token, principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
        stack.grant_sql(principal, "consumer", chain.team)
        card = await stack.create_card(chain.team, token=token, name="orders consumers")
        assert (await stack.subscribe(card["id"], contract, "2.3.0")).status_code == 201
        await deprecate(stack, contract, days=3)

        assert await scheduler(stack).scan_once() == 1  # env leads 30 and 7: lead 7 covers 3 days
        scheduler(stack).settings_client = stored_client({"sunset_lead_days": [3]})
        assert await scheduler(stack).scan_once() == 1  # the stored lead 3 is a new dedupe key
        assert await scheduler(stack).scan_once() == 0  # and is never sent twice
        leads = sorted(
            row["payload"]["lead_days"]
            for row in sql(
                stack.contracts_db_url,
                "SELECT payload FROM card_notice WHERE card_id = %s",
                card["id"],
            )
        )
        assert leads == [3, 7]


# --- N1: stored templates reach the email --------------------------------------------------------


async def test_a_stored_email_template_is_used_by_the_worker(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    mail = type(webhook_sink)()
    mail.respond_json(200, {"accepted": 1, "refused": 0})
    try:
        async with make_stack(**worker_settings(mail_url=f"http://localhost:{mail.port}")) as stack:
            chain = await stack.chain()
            contract, _ = await stack.publish_versions(chain.team, ["2.3.0", "3.0.0"])
            token, principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
            stack.grant_sql(principal, "consumer", chain.team)
            card = await stack.create_card(
                chain.team,
                token=token,
                name="orders consumers",
                contacts=[{"kind": "email", "value": "a@example.test"}],
                email_enabled=True,
            )
            assert (await stack.subscribe(card["id"], contract, "2.3.0")).status_code == 201
            notifier(stack).settings_client = stored_client(
                {
                    "email_templates": {
                        "contract.version.deprecated": {
                            "subject": "{card}: {odcs} is going away",
                            "body": "Heads up {card}\n{details}",
                        }
                    }
                }
            )
            await deprecate(stack, contract)
            assert await stack.pump_notices() == 1
            assert await notifier(stack).run_once() == 1
            (request,) = mail.requests
            sent = json.loads(request.body)
            assert sent["subject"].startswith("orders consumers: ")
            assert sent["subject"].endswith(" is going away")
            assert sent["text"].startswith("Heads up orders consumers\nCard: orders consumers")
    finally:
        mail.stop()


# --- N3: the test email --------------------------------------------------------------------------


def audit_rows(sql: Sql, stack: Any) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT aggregate_type, aggregate_id, scope_path, before, after, actor "
        "FROM outbox WHERE action = 'settings.test_email_sent' ORDER BY id",
    )


async def test_a_global_admin_sends_a_test_email_through_the_mail_service_and_it_is_audited(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    mail = type(webhook_sink)()
    mail.respond_json(200, {"accepted": 1, "refused": 0})
    try:
        async with make_stack(
            mail_url=f"http://localhost:{mail.port}", mail_api_key=SecretStr(new_key())
        ) as stack:
            response = await stack.client.post(
                TEST_EMAIL, json={"to": ADDRESS}, headers=bearer(stack.admin_token)
            )
            assert response.status_code == 200, response.text
            assert response.json() == {"outcome": "sent", "error_code": None}
            (request,) = mail.requests
            # The caller's own bearer, not the notifier key.
            assert request.headers["authorization"] == f"Bearer {stack.admin_token}"
            sent = json.loads(request.body)
            assert sent["to"] == [ADDRESS]
            assert sent["subject"] == "[Contractum] Test email"
            assert "Admin > Settings > Notifications" in sent["text"]
            (event,) = audit_rows(sql, stack)
            assert event["aggregate_type"] == "platform_settings"
            assert event["aggregate_id"] == "notifications"
            assert event["scope_path"] == []
            assert event["before"] is None
            assert event["after"] == {
                "recipient_domain": "example.com",
                "outcome": "sent",
                "error_code": None,
            }
            assert ADDRESS not in json.dumps(event, default=str)
    finally:
        mail.stop()


@pytest.mark.parametrize(
    "answer,outcome,code",
    [
        ((200, {"accepted": 0, "refused": 1}), "refused", "mail-recipients-refused"),
        (
            (200, {"accepted": 0, "refused": 1, "deferred": 1}),
            "unavailable",
            "mail-recipients-deferred",
        ),
        ((503, {"type": "x"}), "unavailable", "mail-http-503"),
        ((403, {"type": "x"}), "refused", "mail-http-403"),
    ],
)
async def test_the_outcome_follows_the_mail_service_answer(
    make_stack: MakeStack,
    sql: Sql,
    webhook_sink: Any,
    answer: tuple[int, Any],
    outcome: str,
    code: str,
) -> None:
    mail = type(webhook_sink)()
    mail.respond_json(*answer)
    try:
        async with make_stack(
            mail_url=f"http://localhost:{mail.port}", mail_api_key=SecretStr(new_key())
        ) as stack:
            response = await stack.client.post(
                TEST_EMAIL, json={"to": ADDRESS}, headers=bearer(stack.admin_token)
            )
            assert response.status_code == 200, response.text
            assert response.json() == {"outcome": outcome, "error_code": code}
            (event,) = audit_rows(sql, stack)
            assert event["after"]["outcome"] == outcome
    finally:
        mail.stop()


async def test_without_a_mail_service_the_outcome_is_not_configured_and_still_audited(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        response = await stack.client.post(
            TEST_EMAIL, json={"to": ADDRESS}, headers=bearer(stack.admin_token)
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"outcome": "not-configured", "error_code": "mail-not-configured"}
        assert len(audit_rows(sql, stack)) == 1


async def test_only_an_interactive_global_admin_may_send_a_test_email(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        editor, principal = stack.token("test-email-editor")
        stack.grant_sql(principal, "team_editor", chain.team)
        refused = await stack.client.post(TEST_EMAIL, json={"to": ADDRESS}, headers=bearer(editor))
        assert refused.status_code == 403, refused.text
        pat = await stack.identity.post(
            "/api/v1/identity/pats",
            json={
                "name": "test-email",
                "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                "read_only": False,
            },
            headers=bearer(stack.admin_token),
        )
        assert pat.status_code == 201, pat.text
        by_pat = await stack.client.post(
            TEST_EMAIL, json={"to": ADDRESS}, headers=bearer(pat.json()["token"])
        )
        assert by_pat.status_code == 403, by_pat.text
        assert by_pat.json()["type"] == "urn:dc:problem:interactive-session-required"
        anonymous = await stack.client.post(TEST_EMAIL, json={"to": ADDRESS})
        assert anonymous.status_code == 401
        assert audit_rows(sql, stack) == []


@pytest.mark.parametrize(
    "body",
    [
        {"to": "not-an-address"},
        {"to": "Name <a@b.com>"},
        {"to": "a@b.com", "from": "x@y.com"},
        {"to": ["a@b.com"]},
        {},
    ],
)
async def test_a_bad_test_email_request_is_422_and_nothing_is_audited(
    make_stack: MakeStack, sql: Sql, body: dict[str, Any]
) -> None:
    async with make_stack() as stack:
        response = await stack.client.post(TEST_EMAIL, json=body, headers=bearer(stack.admin_token))
        assert response.status_code == 422, response.text
        assert "x@y.com" not in response.text
        assert audit_rows(sql, stack) == []


async def test_the_sixth_test_email_in_ten_minutes_is_429_with_retry_after(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    mail = type(webhook_sink)()
    try:
        async with make_stack(
            mail_url=f"http://localhost:{mail.port}", mail_api_key=SecretStr(new_key())
        ) as stack:
            for _ in range(5):
                ok = await stack.client.post(
                    TEST_EMAIL, json={"to": ADDRESS}, headers=bearer(stack.admin_token)
                )
                assert ok.status_code == 200, ok.text
            limited = await stack.client.post(
                TEST_EMAIL, json={"to": ADDRESS}, headers=bearer(stack.admin_token)
            )
            assert limited.status_code == 429, limited.text
            assert limited.json()["type"] == "urn:dc:problem:too-many-attempts"
            assert 1 <= int(limited.headers["retry-after"]) <= 600
            assert limited.json()["retry_after_s"] == int(limited.headers["retry-after"])
            assert len(mail.requests) == 5
            events = audit_rows(sql, stack)
            assert len(events) == 5  # exactly one per attempt that reached the send step
            assert ADDRESS not in json.dumps(events, default=str)
    finally:
        mail.stop()
