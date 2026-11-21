"""Quick 261120-s0p (item 6): the start-up seed of the webhook allow-list, end to end.

Contracts and identity run in one process (the stack builder). Identity provisions the notifier
service account from a credential file, contracts presents its key and posts the hosts of the
existing webhook channels. The seed runs once: a second start adds nothing and a host the admin
removed is never put back.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from cryptography.fernet import Fernet
from dc_contracts import webhooks
from dc_contracts.webhook_host_seed import WebhookHostSeed, run_seed

pytestmark = pytest.mark.integration

CARDS = "/api/v1/contracts/cards"
SETTINGS = "/api/v1/identity/settings/notifications"
KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(webhooks, "_resolve", fake)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def stored(sql: Any, stack: Any) -> list[str] | None:
    rows = sql(
        stack.identity_db_url,
        "SELECT value FROM platform_setting WHERE section = 'notifications'",
    )
    return None if not rows else rows[0]["value"].get("webhook_allowed_hosts")


def seed_events(sql: Any, stack: Any) -> int:
    return len(
        sql(
            stack.identity_db_url,
            "SELECT 1 FROM outbox WHERE action = 'settings.webhook_hosts_seeded'",
        )
    )


async def put_hosts(stack: Any, hosts: list[str], etag: str) -> None:
    response = await stack.identity.put(
        SETTINGS,
        json={"webhook_allowed_hosts": hosts},
        headers={**bearer(stack.admin_token), "If-Match": etag},
    )
    assert response.status_code == 200, response.text


async def wait_done(seed: WebhookHostSeed) -> None:
    for _ in range(100):
        if seed.done:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("the seed was not confirmed")


async def test_the_start_up_hook_seeds_through_the_notifier_key_and_marks_the_seed_done(
    make_stack: Any, sql: Any
) -> None:
    async with make_stack(
        with_notifier=True,
        webhook_secret_key=KEY,
        webhook_host_seed_enabled=True,
        webhook_hosts=None,
    ) as stack:
        seed = stack.contracts_app.state.webhook_host_seed
        await wait_done(seed)
        assert sql(stack.identity_db_url, "SELECT name FROM platform_setting_seed") == [
            {"name": "webhook_hosts"}
        ]
        assert seed_events(sql, stack) == 1
        assert stored(sql, stack) is None  # no channel existed, so nothing to add


async def test_without_a_notifier_key_the_seed_is_done_at_once_and_identity_is_not_called(
    make_stack: Any, sql: Any
) -> None:
    async with make_stack(webhook_host_seed_enabled=True) as stack:
        assert stack.contracts_app.state.webhook_host_seed.done is True
        assert sql(stack.identity_db_url, "SELECT name FROM platform_setting_seed") == []


async def test_with_the_seed_switched_off_it_is_done_at_once_and_identity_is_not_called(
    make_stack: Any, sql: Any
) -> None:
    async with make_stack(with_notifier=True, webhook_host_seed_enabled=False) as stack:
        assert stack.contracts_app.state.webhook_host_seed.done is True
        assert sql(stack.identity_db_url, "SELECT name FROM platform_setting_seed") == []


async def test_the_seed_stores_the_hosts_of_live_channels_once_and_never_puts_one_back(
    make_stack: Any, sql: Any
) -> None:
    async with make_stack(with_notifier=True, webhook_secret_key=KEY) as stack:
        chain = await stack.chain()
        token, principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
        stack.grant_sql(principal, "consumer", chain.team)
        for name, url in (
            ("one", "https://hooks.example.test/a"),
            ("two", "https://hooks.example.test/b"),  # the same host: one entry
            ("off", "https://disabled.example.net/x"),  # not enabled, still a configured channel
            ("gone", "https://archived.example.test/x"),  # archived: not seeded
        ):
            card = await stack.create_card(chain.team, token=token, name=name)
            patched = await stack.client.patch(
                f"{CARDS}/{card['id']}", json={"webhook_url": url}, headers=bearer(token)
            )
            assert patched.status_code == 200, patched.text
            if name == "gone":
                archived = await stack.client.post(
                    f"{CARDS}/{card['id']}/archive", headers=bearer(stack.admin_token)
                )
                assert archived.status_code == 200, archived.text
        # The upgrade: the stack stored a default list; make it the state before the rule existed.
        await put_hosts(stack, [], '"notifications-r1"')
        settings = stack.contracts_app.state.settings

        first = WebhookHostSeed()
        await run_seed(stack.contracts_app, settings, first)
        assert first.done is True
        assert stored(sql, stack) == ["disabled.example.net", "hooks.example.test"]
        assert seed_events(sql, stack) == 1

        # A second start is a no-op, even with the list changed by the admin since.
        await put_hosts(stack, ["disabled.example.net"], '"notifications-r3"')
        second = WebhookHostSeed()
        await run_seed(stack.contracts_app, settings, second)
        assert second.done is True
        assert stored(sql, stack) == ["disabled.example.net"]
        assert seed_events(sql, stack) == 1
