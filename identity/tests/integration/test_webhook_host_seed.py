"""Quick 261120-s0p (item 6): the one-time seed of the webhook allow-list.

``POST /internal/v1/settings/webhook-hosts/seed`` is called by the contracts notifier at start-up
with the hosts of every configured webhook channel. It seeds once (marker row), never widens a
list an admin already stored, never puts back a host that was removed, and never echoes a host.
Refusals: an ordinary user JWT is 403 (a ROOT permission, never 404), no token 401, an oversized
body 422.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration


def _load_notifier_helpers() -> Any:
    name = "dc_identity_tests_bootstrap_notifier"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).with_name("test_bootstrap_notifier.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_notifier = _load_notifier_helpers()
Pair = _notifier.Pair
new_pair = _notifier.new_pair
write_pair = _notifier.write_pair

SEED = "/internal/v1/settings/webhook-hosts/seed"
SETTINGS = "/api/v1/identity/settings"
AdminFlow = Callable[[Any, Any], Awaitable[str]]
Sql = Callable[..., list[dict[str, Any]]]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _stored_hosts(sql: Sql, db_url: str) -> list[str] | None:
    rows = sql(db_url, "SELECT value FROM platform_setting WHERE section = 'notifications'")
    if not rows:
        return None
    hosts = rows[0]["value"].get("webhook_allowed_hosts")
    return None if hosts is None else list(hosts)


def _seed_events(sql: Sql, db_url: str) -> list[dict[str, Any]]:
    return sql(
        db_url,
        "SELECT action, aggregate_type, aggregate_id, actor, before, after, scope_path "
        "FROM outbox WHERE action = 'settings.webhook_hosts_seeded' ORDER BY id",
    )


def _markers(sql: Sql, db_url: str) -> list[str]:
    return [r["name"] for r in sql(db_url, "SELECT name FROM platform_setting_seed")]


@pytest.fixture
def pair(tmp_path: Path) -> Pair:
    made = new_pair()
    write_pair(tmp_path / "notifier_credential", made)
    return made


@pytest.fixture
def env_with_notifier(make_identity: Callable[..., Any], tmp_path: Path, pair: Pair) -> Any:
    del pair
    return make_identity(notifier_credential_file=tmp_path / "notifier_credential")


async def test_first_call_seeds_the_valid_hosts_once_audited_and_never_echoed(
    env_with_notifier: Any, asgi_client: Any, admin_token: AdminFlow, pair: Pair, sql: Sql
) -> None:
    env = env_with_notifier
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        resp = await client.post(
            SEED,
            json={
                "hosts": [
                    "hooks.b.example.com",
                    "hooks.a.example.com",
                    "hooks.b.example.com",
                    "10.0.0.1",
                    "not a host",
                ]
            },
            headers=_bearer(pair.token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"seeded": True, "added": 2, "skipped": 2}
        for host in ("hooks.a.example.com", "10.0.0.1", "not a host"):
            assert host not in resp.text
        assert _stored_hosts(sql, env.db_url) == ["hooks.a.example.com", "hooks.b.example.com"]
        assert _markers(sql, env.db_url) == ["webhook_hosts"]
        events = _seed_events(sql, env.db_url)
        assert len(events) == 1
        event = events[0]
        assert event["aggregate_type"] == "platform_settings"
        assert event["aggregate_id"] == "notifications"
        assert event["actor"]["id"].startswith("sa:")
        assert event["before"] == {"notifications": {}}
        assert event["after"] == {
            "notifications": {
                "webhook_allowed_hosts": ["hooks.a.example.com", "hooks.b.example.com"]
            }
        }
        root = sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"]
        assert [str(i) for i in event["scope_path"]] == [str(root)]
        # The settings cache was cleared: the admin sees the seeded list at once.
        listed = await client.get(SETTINGS, headers=_bearer(admin))
        notifications = next(
            s for s in listed.json()["sections"] if s["section"] == "notifications"
        )
        assert notifications["values"]["webhook_allowed_hosts"] == [
            "hooks.a.example.com",
            "hooks.b.example.com",
        ]
        assert notifications["revision"] == 1


async def test_a_stored_list_is_never_widened_and_only_the_marker_is_written(
    env_with_notifier: Any, asgi_client: Any, admin_token: AdminFlow, pair: Pair, sql: Sql
) -> None:
    env = env_with_notifier
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        put = await client.put(
            f"{SETTINGS}/notifications",
            json={"webhook_allowed_hosts": ["only.example.com"]},
            headers={**_bearer(admin), "If-Match": '"notifications-r0"'},
        )
        assert put.status_code == 200, put.text
        resp = await client.post(
            SEED, json={"hosts": ["extra.example.com"]}, headers=_bearer(pair.token)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"seeded": True, "added": 0, "skipped": 0}
        assert _stored_hosts(sql, env.db_url) == ["only.example.com"]
        assert _markers(sql, env.db_url) == ["webhook_hosts"]
        assert len(_seed_events(sql, env.db_url)) == 1


async def test_later_calls_are_no_ops_so_a_removed_host_is_never_put_back(
    env_with_notifier: Any, asgi_client: Any, admin_token: AdminFlow, pair: Pair, sql: Sql
) -> None:
    env = env_with_notifier
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        first = await client.post(
            SEED, json={"hosts": ["hooks.example.com"]}, headers=_bearer(pair.token)
        )
        assert first.json()["seeded"] is True
        # The admin removes the host (an empty list is stored as no list at all).
        put = await client.put(
            f"{SETTINGS}/notifications",
            json={"webhook_allowed_hosts": []},
            headers={**_bearer(admin), "If-Match": '"notifications-r1"'},
        )
        assert put.status_code == 200, put.text
        before = _stored_hosts(sql, env.db_url)
        again = await client.post(
            SEED,
            json={"hosts": ["hooks.example.com", "new.example.com"]},
            headers=_bearer(pair.token),
        )
        assert again.status_code == 200, again.text
        assert again.json() == {"seeded": False, "added": 0, "skipped": 0}
        assert _stored_hosts(sql, env.db_url) == before
        assert "hooks.example.com" not in (_stored_hosts(sql, env.db_url) or [])
        assert len(_seed_events(sql, env.db_url)) == 1


async def test_two_concurrent_first_calls_seed_once(
    env_with_notifier: Any, asgi_client: Any, pair: Pair, sql: Sql
) -> None:
    env = env_with_notifier
    async with asgi_client(env.app) as client:
        body = {"hosts": ["hooks.example.com"]}
        answers = await asyncio.gather(
            client.post(SEED, json=body, headers=_bearer(pair.token)),
            client.post(SEED, json=body, headers=_bearer(pair.token)),
        )
        assert [a.status_code for a in answers] == [200, 200]
        assert sorted(a.json()["seeded"] for a in answers) == [False, True]
        assert _stored_hosts(sql, env.db_url) == ["hooks.example.com"]
        assert len(_seed_events(sql, env.db_url)) == 1


async def test_more_hosts_than_the_list_holds_are_cut_and_counted_as_skipped(
    env_with_notifier: Any, asgi_client: Any, pair: Pair, sql: Sql
) -> None:
    env = env_with_notifier
    hosts = [f"h{i:03d}.example.com" for i in range(130)]
    async with asgi_client(env.app) as client:
        resp = await client.post(SEED, json={"hosts": hosts}, headers=_bearer(pair.token))
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"seeded": True, "added": 100, "skipped": 30}
        assert _stored_hosts(sql, env.db_url) == hosts[:100]


async def test_an_empty_first_call_still_writes_the_marker_so_it_is_once_only(
    env_with_notifier: Any, asgi_client: Any, pair: Pair, sql: Sql
) -> None:
    env = env_with_notifier
    async with asgi_client(env.app) as client:
        resp = await client.post(SEED, json={"hosts": []}, headers=_bearer(pair.token))
        assert resp.json() == {"seeded": True, "added": 0, "skipped": 0}
        assert _markers(sql, env.db_url) == ["webhook_hosts"]
        later = await client.post(
            SEED, json={"hosts": ["hooks.example.com"]}, headers=_bearer(pair.token)
        )
        assert later.json() == {"seeded": False, "added": 0, "skipped": 0}
        assert _stored_hosts(sql, env.db_url) in (None, [])


async def test_callers_without_the_permission_are_refused_and_nothing_is_written(
    env_with_notifier: Any,
    asgi_client: Any,
    admin_token: AdminFlow,
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
    grant_sql: Callable[..., None],
    folder_chain: Callable[..., Awaitable[tuple[str, str, str]]],
    pair: Pair,
    sql: Sql,
) -> None:
    env = env_with_notifier
    body = {"hosts": ["hooks.example.com"]}
    async with asgi_client(env.app) as client:
        admin = await admin_token(client, env)
        root = sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"]
        _org, _project, team = await folder_chain(client, admin, root, "seed")
        user = await create_user(client, admin, "carol")
        grant_sql(env.db_url, user.principal_id, "consumer", team)
        token = await login(client, user.username, user.password)
        assert (await client.post(SEED, json=body, headers=_bearer(token))).status_code == 403
        assert (await client.post(SEED, json=body)).status_code == 401
        bad = await client.post(SEED, json=body, headers=_bearer(pair.token[:-3] + "xyz"))
        assert bad.status_code == 401
        assert _markers(sql, env.db_url) == []
        assert _stored_hosts(sql, env.db_url) is None


async def test_an_oversized_body_is_422(
    env_with_notifier: Any, asgi_client: Any, pair: Pair
) -> None:
    env = env_with_notifier
    async with asgi_client(env.app) as client:
        too_many = await client.post(
            SEED, json={"hosts": ["a.example.com"] * 1001}, headers=_bearer(pair.token)
        )
        assert too_many.status_code == 422
        too_long = await client.post(SEED, json={"hosts": ["a" * 254]}, headers=_bearer(pair.token))
        assert too_long.status_code == 422
        extra = await client.post(SEED, json={"hosts": [], "other": 1}, headers=_bearer(pair.token))
        assert extra.status_code == 422
        assert json.loads(too_many.text)["type"].endswith("validation-error")
