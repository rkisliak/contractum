"""Webhook channel configuration (SUB-07, D-98, D-107, D-79): URL rules, the shown-once secret.

A card manager turns the webhook channel on for a safe URL and receives the ``whsec_`` signing
secret exactly once; the secret is encrypted at rest in its own table and never reaches an audit
event, a log or a later response. Issuing the first secret is credential management, so it needs an
interactive sign-in: the identity PDP refuses a PAT or a key (``card:manage-secret``).
"""

from __future__ import annotations

import base64
import json
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
URL = "https://hooks.example.test/h"
KEY = Fernet.generate_key().decode()

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

HOSTS: dict[str, list[str]] = {
    "hooks.example.test": ["93.184.216.34"],
    "other.example.test": ["93.184.216.35"],
    "internal.example.test": ["10.0.0.1"],
    "mixed.example.test": ["93.184.216.34", "10.0.0.1"],
    "mapped.example.test": ["::ffff:10.0.0.1"],
    "metadata.example.test": ["169.254.169.254"],
    "localhost": ["127.0.0.1"],
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def assert_problem(response: httpx.Response, status: int, slug: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert body["type"] == f"urn:dc:problem:{slug}"
    assert body["status"] == status
    return body


@pytest.fixture(autouse=True)
def resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one resolver seam: no test touches real DNS."""

    async def fake(host: str, port: int) -> list[str]:
        if host in HOSTS:
            return list(HOSTS[host])
        raise OSError("no such host")

    monkeypatch.setattr(webhooks, "_resolve", fake)


async def _manager_card(stack: Any) -> tuple[Any, dict[str, Any], str]:
    """A chain, a card created by a Consumer (so its manager) and that Consumer's token."""
    chain = await stack.chain()
    token, principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal, "consumer", chain.team)
    card = await stack.create_card(chain.team, token=token)
    return chain, card, token


async def _patch(stack: Any, card_id: str, token: str, **body: Any) -> httpx.Response:
    return await stack.client.patch(f"{CARDS}/{card_id}", json=body, headers=bearer(token))


async def _get(stack: Any, card_id: str, token: str) -> httpx.Response:
    return await stack.client.get(f"{CARDS}/{card_id}", headers=bearer(token))


def _secret_rows(sql: Sql, stack: Any) -> list[dict[str, Any]]:
    return sql(stack.contracts_db_url, "SELECT * FROM card_webhook_secret ORDER BY created_at")


def _outbox(sql: Sql, stack: Any, action: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT before, after FROM outbox WHERE action = %s ORDER BY id",
        action,
    )


def _decoded(secret: str) -> bytes:
    assert secret.startswith("whsec_")
    return base64.b64decode(secret.removeprefix("whsec_"), validate=True)


async def _read_write_pat(stack: Any, *, read_only: bool = False) -> str:
    created = await stack.identity.post(
        "/api/v1/identity/pats",
        json={
            "name": "webhook-test",
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
            "read_only": read_only,
        },
        headers=bearer(stack.admin_token),
    )
    assert created.status_code == 201, created.text
    return str(created.json()["token"])


# --- tracer: a manager enables a webhook for a safe URL and gets the secret exactly once -------


async def test_a_manager_enables_a_webhook_and_receives_the_secret_exactly_once(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        _, card, manager = await _manager_card(stack)

        enabled = await _patch(stack, card["id"], manager, webhook_url=URL, webhook_enabled=True)
        assert enabled.status_code == 200, enabled.text
        secret = enabled.json()["webhook_secret"]
        assert len(_decoded(secret)) == 32
        assert enabled.json()["webhook_enabled"] is True
        assert enabled.json()["webhook_secret_configured"] is True

        read = await _get(stack, card["id"], manager)
        assert read.status_code == 200, read.text
        assert read.json()["webhook_secret_configured"] is True
        assert "webhook_secret" not in read.json()
        assert secret not in read.text

        (row,) = _secret_rows(sql, stack)
        assert row["generation"] == 1
        assert row["previous_enc"] is None
        assert secret not in row["secret_enc"]
        assert secret.removeprefix("whsec_") not in row["secret_enc"]
        assert Fernet(KEY).decrypt(row["secret_enc"].encode()).decode() == secret


async def test_the_audit_event_carries_the_host_and_generation_never_the_secret_or_url(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        _, card, manager = await _manager_card(stack)
        enabled = await _patch(stack, card["id"], manager, webhook_url=URL, webhook_enabled=True)
        secret = enabled.json()["webhook_secret"]
        (row,) = _secret_rows(sql, stack)

        events = _outbox(sql, stack, "card.updated")
        assert len(events) == 1
        after = events[0]["after"]
        assert after["webhook_host"] == "hooks.example.test"
        assert after["webhook_secret_generation"] == 1
        text = json.dumps(events[0])
        for forbidden in (secret, secret.removeprefix("whsec_"), row["secret_enc"], "/h", "https:"):
            assert forbidden not in text, forbidden


async def test_disabling_keeps_the_secret_and_re_enabling_returns_no_new_one(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        _, card, manager = await _manager_card(stack)
        first = await _patch(stack, card["id"], manager, webhook_url=URL, webhook_enabled=True)
        before = _secret_rows(sql, stack)

        off = await _patch(stack, card["id"], manager, webhook_enabled=False)
        assert off.status_code == 200, off.text
        assert off.json().get("webhook_secret") is None
        assert off.json()["webhook_enabled"] is False
        assert off.json()["webhook_secret_configured"] is True

        on = await _patch(stack, card["id"], manager, webhook_enabled=True)
        assert on.status_code == 200, on.text
        assert on.json().get("webhook_secret") is None
        assert on.json()["webhook_enabled"] is True

        after = _secret_rows(sql, stack)
        assert after == before
        assert after[0]["generation"] == 1
        assert first.json()["webhook_secret"] not in json.dumps(on.json())


async def test_changing_the_url_revalidates_it_and_keeps_the_secret(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        _, card, manager = await _manager_card(stack)
        await _patch(stack, card["id"], manager, webhook_url=URL, webhook_enabled=True)
        before = _secret_rows(sql, stack)

        moved = await _patch(stack, card["id"], manager, webhook_url="https://other.example.test/x")
        assert moved.status_code == 200, moved.text
        assert moved.json().get("webhook_secret") is None
        assert moved.json()["webhook_url"] == "https://other.example.test/x"
        assert _secret_rows(sql, stack) == before

        bad = await _patch(stack, card["id"], manager, webhook_url="https://internal.example.test/")
        assert_problem(bad, 422, "invalid-webhook-url")
        kept = await _get(stack, card["id"], manager)
        assert kept.json()["webhook_url"] == "https://other.example.test/x"

        events = _outbox(sql, stack, "card.updated")
        assert events[-1]["after"]["webhook_host"] == "other.example.test"
        assert events[-1]["after"]["webhook_secret_generation"] == 1


async def test_setting_a_url_without_enabling_creates_no_secret(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        _, card, manager = await _manager_card(stack)
        stored = await _patch(stack, card["id"], manager, webhook_url=URL)
        assert stored.status_code == 200, stored.text
        assert stored.json().get("webhook_secret") is None
        assert stored.json()["webhook_enabled"] is False
        assert stored.json()["webhook_secret_configured"] is False
        assert _secret_rows(sql, stack) == []


# --- URL rules (SSRF guard at write time, T-04.1-27) -------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://hooks.example.test/h",
        "http://hooks.example.test/h",
        "https://user:pw@hooks.example.test/h",
        "https://@hooks.example.test/h",
        "https:///nohost",
        "https://hooks.example.test/" + "a" * 2048,
        "https://hooks.example.test/a b",
        "https://hooks.example.test/a\tb",
        "https://internal.example.test/h",
        "https://mixed.example.test/h",
        "https://mapped.example.test/h",
        "https://metadata.example.test/h",
        "https://10.0.0.1/h",
        "https://[::1]/h",
        "https://unknown.example.test/h",
        "not a url",
    ],
)
async def test_an_unsafe_url_is_refused_without_echoing_it(
    make_stack: MakeStack, sql: Sql, url: str
) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        _, card, manager = await _manager_card(stack)
        refused = await _patch(stack, card["id"], manager, webhook_url=url, webhook_enabled=True)
        assert_problem(refused, 422, "invalid-webhook-url")
        assert url not in refused.text
        assert "example.test" not in refused.text
        unchanged = await _get(stack, card["id"], manager)
        assert unchanged.json()["webhook_url"] is None
        assert unchanged.json()["webhook_enabled"] is False
        assert _secret_rows(sql, stack) == []
        assert _outbox(sql, stack, "card.updated") == []


async def test_enabling_without_a_url_is_refused(make_stack: MakeStack, sql: Sql) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        _, card, manager = await _manager_card(stack)
        refused = await _patch(stack, card["id"], manager, webhook_enabled=True)
        assert_problem(refused, 422, "invalid-webhook-url")
        assert _secret_rows(sql, stack) == []


async def test_http_is_accepted_only_when_https_is_not_required(make_stack: MakeStack) -> None:
    async with make_stack(webhook_secret_key=KEY, webhook_require_https=False) as stack:
        _, card, manager = await _manager_card(stack)
        accepted = await _patch(
            stack, card["id"], manager, webhook_url="http://hooks.example.test/h"
        )
        assert accepted.status_code == 200, accepted.text


async def test_an_allow_listed_private_host_is_accepted(make_stack: MakeStack) -> None:
    async with make_stack(
        webhook_secret_key=KEY, webhook_allowed_private_hosts=["LocalHost"]
    ) as stack:
        _, card, manager = await _manager_card(stack)
        accepted = await _patch(
            stack, card["id"], manager, webhook_url="https://localhost/h", webhook_enabled=True
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["webhook_secret"].startswith("whsec_")
        other = await _patch(
            stack, card["id"], manager, webhook_url="https://internal.example.test/"
        )
        assert_problem(other, 422, "invalid-webhook-url")


async def test_enabling_without_the_encryption_key_is_unavailable_and_stores_nothing(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        _, card, manager = await _manager_card(stack)
        unavailable = await _patch(
            stack, card["id"], manager, webhook_url=URL, webhook_enabled=True
        )
        assert_problem(unavailable, 503, "service-unavailable")
        assert _secret_rows(sql, stack) == []
        read = await _get(stack, card["id"], manager)
        assert read.json()["webhook_enabled"] is False
        assert read.json()["webhook_url"] is None
        assert _outbox(sql, stack, "card.updated") == []


# --- D-79: the first secret needs an interactive sign-in ----------------------------------------


async def test_a_pat_cannot_issue_the_first_secret_but_may_toggle_an_existing_one(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        chain = await stack.chain()
        card = await stack.create_card(chain.team)
        pat = await _read_write_pat(stack)

        refused = await _patch(stack, card["id"], pat, webhook_url=URL, webhook_enabled=True)
        assert_problem(refused, 403, "interactive-session-required")
        unchanged = await _get(stack, card["id"], stack.admin_token)
        assert unchanged.json()["webhook_enabled"] is False
        assert unchanged.json()["webhook_url"] is None
        assert _secret_rows(sql, stack) == []
        assert _outbox(sql, stack, "card.updated") == []

        issued = await _patch(
            stack, card["id"], stack.admin_token, webhook_url=URL, webhook_enabled=True
        )
        assert issued.status_code == 200, issued.text
        assert issued.json()["webhook_secret"].startswith("whsec_")

        off = await _patch(stack, card["id"], pat, webhook_enabled=False)
        assert off.status_code == 200, off.text
        assert off.json().get("webhook_secret") is None
        on = await _patch(stack, card["id"], pat, webhook_enabled=True)
        assert on.status_code == 200, on.text
        assert on.json().get("webhook_secret") is None
        assert len(_secret_rows(sql, stack)) == 1


async def test_a_pat_may_still_edit_the_url_without_enabling(make_stack: MakeStack) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        chain = await stack.chain()
        card = await stack.create_card(chain.team)
        pat = await _read_write_pat(stack)
        stored = await _patch(stack, card["id"], pat, webhook_url=URL)
        assert stored.status_code == 200, stored.text
        assert stored.json()["webhook_url"] == URL


# --- T-04.1-56: tokens in a URL are shown to managers only --------------------------------------


async def test_only_managers_see_the_full_url_everyone_else_sees_scheme_and_host(
    make_stack: MakeStack,
) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        chain, card, manager = await _manager_card(stack)
        # x, q, h, o, p and r are not hex digits: the path marker never turns up by chance in an id.
        secret_url = "https://hooks.example.test/services/T000/B000/xq-path-probe?token=s3cret"
        stored = await _patch(
            stack, card["id"], manager, webhook_url=secret_url, webhook_enabled=True
        )
        assert stored.status_code == 200, stored.text
        assert stored.json()["webhook_url"] == secret_url

        as_manager = await _get(stack, card["id"], manager)
        assert as_manager.json()["webhook_url"] == secret_url

        reader, reader_principal = stack.token("reader-url")
        stack.grant_sql(reader_principal, "team_reader", chain.team)
        as_reader = await _get(stack, card["id"], reader)
        assert as_reader.status_code == 200, as_reader.text
        assert as_reader.json()["webhook_url"] == "https://hooks.example.test"
        assert "xq-path-probe" not in as_reader.text
        assert "s3cret" not in as_reader.text

        group = await stack.local_group(stack.root_id, f"g-{uuid.uuid4().hex[:6]}")
        member, member_principal = stack.token("member-url")
        stack.group_member_sql(group, member_principal)
        assert (await stack.link_member(card["id"], "local_group", group, token=manager)).is_success
        as_member = await _get(stack, card["id"], member)
        assert as_member.status_code == 200, as_member.text
        assert as_member.json()["webhook_url"] == "https://hooks.example.test"
        assert "xq-path-probe" not in as_member.text


# --- rotation: an overlap window, managers signed in interactively only (D-98, D-79, D-85) -------


def _rotate_url(card_id: str) -> str:
    return f"{CARDS}/{card_id}/webhook-secret"


async def _rotate(stack: Any, card_id: str, token: str) -> httpx.Response:
    return await stack.client.post(_rotate_url(card_id), headers=bearer(token))


async def _enabled_card(stack: Any) -> tuple[Any, dict[str, Any], str, str]:
    """A manager's card with the webhook enabled; returns chain, card, manager token, secret."""
    chain, card, manager = await _manager_card(stack)
    enabled = await _patch(stack, card["id"], manager, webhook_url=URL, webhook_enabled=True)
    assert enabled.status_code == 200, enabled.text
    return chain, card, manager, str(enabled.json()["webhook_secret"])


async def test_a_manager_rotates_the_secret_and_the_previous_one_stays_for_the_overlap(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(webhook_secret_key=KEY, webhook_secret_overlap_s=600) as stack:
        _, card, manager, first = await _enabled_card(stack)

        before = datetime.now(UTC)
        rotated = await _rotate(stack, card["id"], manager)
        assert rotated.status_code == 200, rotated.text
        body = rotated.json()
        second = body["webhook_secret"]
        assert len(_decoded(second)) == 32
        assert second != first
        assert body["generation"] == 2
        until = datetime.fromisoformat(body["previous_valid_until"])
        assert abs((until - (before + timedelta(seconds=600))).total_seconds()) < 30

        (row,) = _secret_rows(sql, stack)
        assert row["generation"] == 2
        assert Fernet(KEY).decrypt(row["previous_enc"].encode()).decode() == first
        assert Fernet(KEY).decrypt(row["secret_enc"].encode()).decode() == second
        assert row["previous_valid_until"] == until

        events = _outbox(sql, stack, "card.webhook_secret_rotated")
        assert len(events) == 1
        assert events[0]["before"] == {"generation": 1}
        assert events[0]["after"] == {"generation": 2}
        text = json.dumps(events[0])
        for forbidden in ("whsec_", first, second, row["secret_enc"], row["previous_enc"]):
            assert forbidden not in text, forbidden


async def test_a_second_rotation_keeps_only_one_previous_secret(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        _, card, manager, _ = await _enabled_card(stack)
        second = (await _rotate(stack, card["id"], manager)).json()["webhook_secret"]
        third = await _rotate(stack, card["id"], manager)
        assert third.status_code == 200, third.text
        assert third.json()["generation"] == 3
        (row,) = _secret_rows(sql, stack)
        assert Fernet(KEY).decrypt(row["previous_enc"].encode()).decode() == second
        assert (
            Fernet(KEY).decrypt(row["secret_enc"].encode()).decode()
            == third.json()["webhook_secret"]
        )
        assert len(_outbox(sql, stack, "card.webhook_secret_rotated")) == 2


async def test_a_pat_or_a_key_cannot_rotate_and_learns_nothing_about_card_ids(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        chain = await stack.chain()
        card = await stack.create_card(chain.team)
        issued = await _patch(
            stack, card["id"], stack.admin_token, webhook_url=URL, webhook_enabled=True
        )
        assert issued.status_code == 200, issued.text
        pat = await _read_write_pat(stack)
        read_only = await _read_write_pat(stack, read_only=True)
        key, account = await stack.service_account_key(stack.root_id, "pipeline-bot")
        group = await stack.local_group(stack.root_id, f"g-{uuid.uuid4().hex[:6]}")
        assert (await stack.link_member(card["id"], "local_group", group)).is_success
        assert (await stack.link_member(card["id"], "service_account", account)).is_success

        known = await _rotate(stack, card["id"], pat)
        assert_problem(known, 403, "interactive-session-required")
        unknown = await _rotate(stack, str(uuid.uuid4()), pat)
        assert_problem(unknown, 403, "interactive-session-required")
        keep = ("type", "title", "status", "detail")
        assert {k: known.json().get(k) for k in keep} == {k: unknown.json().get(k) for k in keep}

        by_key = await _rotate(stack, card["id"], key)
        assert_problem(by_key, 403, "interactive-session-required")
        assert_problem(await _rotate(stack, card["id"], read_only), 403, "forbidden")
        assert _secret_rows(sql, stack)[0]["generation"] == 1
        assert _outbox(sql, stack, "card.webhook_secret_rotated") == []


async def test_only_a_manager_signed_in_interactively_may_rotate(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        _, card, manager, _ = await _enabled_card(stack)
        group = await stack.local_group(stack.root_id, f"g-{uuid.uuid4().hex[:6]}")
        dana, dana_principal = stack.token("dana-rotate")
        stack.group_member_sql(group, dana_principal)
        assert (await stack.link_member(card["id"], "local_group", group, token=manager)).is_success
        outsider, _ = stack.token("outsider-rotate")

        assert_problem(await _rotate(stack, card["id"], dana), 403, "forbidden")
        assert_problem(await _rotate(stack, card["id"], outsider), 404, "not-found")
        assert_problem(await _rotate(stack, str(uuid.uuid4()), manager), 404, "not-found")
        unauthenticated = await stack.client.post(_rotate_url(card["id"]))
        assert unauthenticated.status_code == 401, unauthenticated.text
        assert _secret_rows(sql, stack)[0]["generation"] == 1


async def test_rotation_needs_a_url_and_a_live_card(make_stack: MakeStack, sql: Sql) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        _, bare, manager = await _manager_card(stack)
        assert_problem(await _rotate(stack, bare["id"], manager), 409, "webhook-not-configured")
        assert _secret_rows(sql, stack) == []

        _, card, owner, _ = await _enabled_card(stack)
        archived = await stack.client.post(f"{CARDS}/{card['id']}/archive", headers=bearer(owner))
        assert archived.status_code == 200, archived.text
        assert_problem(await _rotate(stack, card["id"], owner), 409, "card-archived")
        assert _secret_rows(sql, stack)[0]["generation"] == 1


async def test_rotation_without_the_encryption_key_is_unavailable(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        _, card, manager = await _manager_card(stack)
        assert_problem(await _rotate(stack, card["id"], manager), 503, "service-unavailable")


async def test_no_read_or_later_edit_ever_returns_a_secret(make_stack: MakeStack) -> None:
    async with make_stack(webhook_secret_key=KEY) as stack:
        _, card, manager, first = await _enabled_card(stack)
        second = (await _rotate(stack, card["id"], manager)).json()["webhook_secret"]
        responses = [
            await _get(stack, card["id"], manager),
            await stack.client.get(f"{CARDS}/{card['id']}/notices", headers=bearer(manager)),
            await _patch(stack, card["id"], manager, description="changed"),
            await _patch(stack, card["id"], manager, webhook_enabled=False),
            await _patch(stack, card["id"], manager, webhook_enabled=True),
            await _patch(stack, card["id"], manager, webhook_url="https://other.example.test/x"),
        ]
        for response in responses:
            assert response.status_code == 200, response.text
            assert "whsec_" not in response.text
            assert first not in response.text
            assert second not in response.text


async def test_the_dns_check_runs_before_the_card_row_is_locked(
    make_stack: MakeStack, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    # WR-02: a slow resolver must not hold the card's FOR UPDATE lock (and queue other writers).
    import asyncio

    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(host: str, port: int) -> list[str]:
        entered.set()
        await release.wait()
        return ["93.184.216.34"]

    monkeypatch.setattr(webhooks, "_resolve", slow)
    async with make_stack(webhook_secret_key=KEY) as stack:
        _, card, manager = await _manager_card(stack)
        patch = asyncio.create_task(
            _patch(stack, card["id"], manager, webhook_url=URL, webhook_enabled=True)
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            # NOWAIT raises (and fails the test) when the patch already holds the row lock.
            sql(
                stack.contracts_db_url,
                "SELECT id FROM subscriber_card WHERE id = %s FOR UPDATE NOWAIT",
                card["id"],
            )
        finally:
            release.set()
        response = await patch
        assert response.status_code == 200, response.text


async def test_a_resolver_that_never_answers_is_a_422_not_a_hang(
    make_stack: MakeStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    async def hangs(host: str, port: int) -> list[str]:
        await asyncio.sleep(3600)
        return []

    monkeypatch.setattr(webhooks, "_resolve", hangs)
    async with make_stack(webhook_secret_key=KEY, webhook_timeout_s=0.3) as stack:
        _, card, manager = await _manager_card(stack)
        response = await _patch(stack, card["id"], manager, webhook_url=URL)
        assert_problem(response, 422, "invalid-webhook-url")
