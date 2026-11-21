"""SUB-08 (D-162..D-164): a breaking publish notifies the cards pinned to an affected older version.

The publish event snapshots the pins (`notify_pins`); the notice consumer (reached here through
`Stack.pump_notices`) diffs each pinned version against the new one and records one
`contract.version.breaking_published` notice per affected card, in addition to the ordinary
`contract.version.published` notice, then the delivery worker sends it like any other notice.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from cryptography.fernet import Fernet
from dc_contracts import webhooks
from dc_contracts.migrate import SCRIPT_LOCATION, VERSION_TABLE
from dc_core.migrations import run_alembic

pytestmark = pytest.mark.integration

CARDS = "/api/v1/contracts/cards"
CONTRACTS = "/api/v1/contracts/contracts"
KEY = Fernet.generate_key().decode()
BREAKING = "contract.version.breaking_published"
PUBLISHED = "contract.version.published"

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

SETTINGS: dict[str, Any] = {
    "webhook_secret_key": KEY,
    "webhook_require_https": False,
    "webhook_allowed_private_hosts": ["localhost"],
    # The worker reads the mandatory allow-list from identity with the notifier key.
    "with_notifier": True,
}


@pytest.fixture(autouse=True)
def resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one resolver seam: ``localhost`` is the sink, nothing else resolves."""

    async def fake(host: str, port: int) -> list[str]:
        if host == "localhost":
            return ["127.0.0.1"]
        raise OSError("no such host")

    monkeypatch.setattr(webhooks, "_resolve", fake)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, version: str, *names: str, description: str = "") -> bytes:
    """A conformant ODCS v3.2.0 document with one table holding the named string properties."""
    props = "".join(f"      - name: {n}\n        logicalType: string\n" for n in names)
    text = f"description:\n  purpose: {description}\n" if description else ""
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        f"status: draft\nname: Orders\n{text}schema:\n  - name: orders\n    properties:\n{props}"
    ).encode()


async def publish_next(
    stack: Any,
    contract: str,
    odcs_id: str,
    version: str,
    names: tuple[str, ...],
    *,
    base: str,
    description: str = "",
) -> Any:
    created = await stack.client.post(
        f"{CONTRACTS}/{contract}/drafts",
        json={"from_version": base, "bump": "none"},
        headers=bearer(stack.admin_token),
    )
    assert created.status_code == 201, created.text
    draft = created.json()["draft"]
    saved = await stack.client.put(
        f"{CONTRACTS}/{contract}/drafts/{draft['id']}",
        content=doc(odcs_id, version, *names, description=description),
        headers={
            **bearer(stack.admin_token),
            "Content-Type": "application/yaml",
            "If-Match": draft["etag"],
        },
    )
    assert saved.status_code == 200, saved.text
    return await stack.publish(contract, draft["id"], saved.headers["etag"])


async def first_publish(stack: Any, team: str, odcs_id: str, names: tuple[str, ...]) -> str:
    contract, draft, etag = await stack.create_contract(team, doc(odcs_id, "1.0.0", *names))
    assert (await stack.publish(contract, draft, etag)).status_code == 201
    return contract


def notices_of(sql: Sql, stack: Any, card_id: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT * FROM card_notice WHERE card_id = %s ORDER BY id",
        card_id,
    )


def outbox(sql: Sql, stack: Any, action: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT event_id, after FROM outbox WHERE action = %s ORDER BY id",
        action,
    )


def webhook_card_manager(stack: Any, team: str) -> tuple[str, str]:
    token, principal = stack.token(f"mgr-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal, "consumer", team)
    return token, principal


async def webhook_card(stack: Any, team: str, sink: Any, name: str) -> tuple[dict[str, Any], str]:
    token, _ = webhook_card_manager(stack, team)
    card = await stack.create_card(team, token=token, name=name)
    enabled = await stack.client.patch(
        f"{CARDS}/{card['id']}",
        json={"webhook_url": sink.url("/hooks"), "webhook_enabled": True},
        headers=bearer(token),
    )
    assert enabled.status_code == 200, enabled.text
    return card, token


# --- tracer: a card pinned to 1.0.0 is told when 2.0.0 removes a column --------------------------


async def test_a_card_pinned_to_an_older_version_gets_a_breaking_notice_and_a_webhook(
    make_stack: MakeStack, sql: Sql, webhook_sink: Any
) -> None:
    async with make_stack(**SETTINGS) as stack:
        chain = await stack.chain()
        odcs_id = f"urn:dc:breaking:{uuid.uuid4().hex}"
        contract = await first_publish(stack, chain.team, odcs_id, ("id", "amount"))
        card, _ = await webhook_card(stack, chain.team, webhook_sink, "orders consumers")
        pinned = await stack.subscribe(card["id"], contract, "1.0.0")
        assert pinned.status_code == 201, pinned.text
        await stack.pump_notices()

        published = await publish_next(stack, contract, odcs_id, "2.0.0", ("id",), base="1.0.0")
        assert published.status_code == 201, published.text

        # The 2.0.0 event snapshots the pin: identifiers only, read under the publish locks.
        (_, event) = outbox(sql, stack, "contract.version_published")
        assert event["after"]["notify_pins"] == [
            {"card_id": card["id"], "version_id": pinned.json()["version_id"]}
        ]
        assert event["after"]["notify_card_ids"] == [card["id"]]

        assert await stack.pump_notices() == 2
        by_type = {n["event_type"]: n for n in notices_of(sql, stack, card["id"])}
        assert set(by_type) == {PUBLISHED, BREAKING}
        assert by_type[BREAKING]["dedupe_key"] == f"event:{event['event_id']}:breaking"
        assert by_type[PUBLISHED]["dedupe_key"] == f"event:{event['event_id']}"

        payload = by_type[BREAKING]["payload"]
        assert payload["version"]["semver"] == "2.0.0"
        (section,) = payload["pinned_versions"]
        assert section["semver"] == "1.0.0"
        assert section["breaking_count"] == 1
        assert [c["code"] for c in section["changes"]] == ["property-removed"]
        assert section["changes"][0]["path"] == "schema.orders.properties.amount"
        assert section["diff_url"] == (
            f"/api/v1/contracts/contracts/{contract}/diff?from=1.0.0&to=2.0.0"
        )

        # The webhook goes through the existing delivery path: one request per notice.
        assert await stack.contracts_app.state.card_notifier.run_once() == 2
        types = sorted(json.loads(r.body)["type"] for r in webhook_sink.requests)
        assert types == [BREAKING, PUBLISHED]
        (sent,) = [r for r in webhook_sink.requests if json.loads(r.body)["type"] == BREAKING]
        data = json.loads(sent.body)["data"]
        assert data["pinned_versions"][0]["breaking_count"] == 1

        # Redelivering the same event records nothing more.
        assert await stack.replay_event(event["event_id"]) == 0
        assert len(notices_of(sql, stack, card["id"])) == 2


# --- who is and is not told ----------------------------------------------------------------------


def keys_of(value: Any) -> set[str]:
    """Every dict key in a JSON value, recursively."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found |= {key} | keys_of(item)
    elif isinstance(value, list):
        for item in value:
            found |= keys_of(item)
    return found


def types_of(sql: Sql, stack: Any, card_id: str) -> list[str]:
    return [n["event_type"] for n in notices_of(sql, stack, card_id)]


async def test_a_breaking_publish_with_no_pinned_cards_creates_no_breaking_notice(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = f"urn:dc:breaking:{uuid.uuid4().hex}"
        contract = await first_publish(stack, chain.team, odcs_id, ("id", "amount"))
        card = await stack.create_card(chain.team)  # a card that pins nothing
        published = await publish_next(stack, contract, odcs_id, "2.0.0", ("id",), base="1.0.0")
        assert published.status_code == 201, published.text

        assert await stack.pump_notices() == 0
        assert types_of(sql, stack, card["id"]) == []
        (_, event) = outbox(sql, stack, "contract.version_published")
        assert event["after"]["notify_pins"] == []


async def test_a_non_breaking_publish_notifies_the_pinned_card_without_a_breaking_notice(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = f"urn:dc:breaking:{uuid.uuid4().hex}"
        contract = await first_publish(stack, chain.team, odcs_id, ("id", "amount"))
        card = await stack.create_card(chain.team)
        assert (await stack.subscribe(card["id"], contract, "1.0.0")).status_code == 201
        # A description change and a new optional column break nobody.
        published = await publish_next(
            stack,
            contract,
            odcs_id,
            "1.1.0",
            ("id", "amount", "note"),
            base="1.0.0",
            description="new words",
        )
        assert published.status_code == 201, published.text

        await stack.pump_notices()
        assert types_of(sql, stack, card["id"]) == [PUBLISHED]


async def test_a_card_pinned_to_two_breaking_versions_gets_one_notice_with_a_section_each(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = f"urn:dc:breaking:{uuid.uuid4().hex}"
        contract = await first_publish(stack, chain.team, odcs_id, ("id", "amount", "extra"))
        only_old = await stack.create_card(chain.team)
        both = await stack.create_card(chain.team)
        assert (await stack.subscribe(only_old["id"], contract, "1.0.0")).status_code == 201
        assert (await stack.subscribe(both["id"], contract, "1.0.0")).status_code == 201
        minor = await publish_next(
            stack, contract, odcs_id, "1.10.0", ("id", "amount", "extra", "more"), base="1.0.0"
        )
        assert minor.status_code == 201, minor.text
        assert (await stack.subscribe(both["id"], contract, "1.10.0")).status_code == 201
        published = await publish_next(stack, contract, odcs_id, "2.0.0", ("id",), base="1.10.0")
        assert published.status_code == 201, published.text

        await stack.pump_notices()
        breaking_old = [
            n for n in notices_of(sql, stack, only_old["id"]) if n["event_type"] == BREAKING
        ]
        breaking_both = [
            n for n in notices_of(sql, stack, both["id"]) if n["event_type"] == BREAKING
        ]
        assert len(breaking_old) == 1 and len(breaking_both) == 1
        old_sections = breaking_old[0]["payload"]["pinned_versions"]
        sections = breaking_both[0]["payload"]["pinned_versions"]
        assert [s["semver"] for s in old_sections] == ["1.0.0"]
        # semver precedence (1.0.0 before 1.10.0), not text order
        assert [s["semver"] for s in sections] == ["1.0.0", "1.10.0"]
        assert [s["breaking_count"] for s in sections] == [2, 3]
        assert breaking_both[0]["payload"]["truncated_pins"] == 0
        assert not {"description", "examples", "quality", "query", "customProperties"} & keys_of(
            breaking_both[0]["payload"]
        )


# --- migration 0016 ------------------------------------------------------------------------------


def _run(url: str, command: str, revision: str = "head") -> None:
    run_alembic(
        script_location=SCRIPT_LOCATION,
        version_table=VERSION_TABLE,
        url=url,
        command=command,  # type: ignore[arg-type]
        revision=revision,
    )


def _check_text(url: str, sql: Sql) -> str:
    rows = sql(
        url,
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conname = 'ck_card_notice_event_type_known' OR "
        "(conrelid = 'card_notice'::regclass AND contype = 'c')",
    )
    return " ".join(r["def"] for r in rows)


def test_migration_0016_widens_the_event_type_check_and_its_downgrade_restores_it(
    fresh_db_url: str, sql: Sql
) -> None:
    _run(fresh_db_url, "upgrade", "0015_rule_set_attachments")
    assert BREAKING not in _check_text(fresh_db_url, sql)

    _run(fresh_db_url, "upgrade", "0016_breaking_notice_type")
    assert BREAKING in _check_text(fresh_db_url, sql)

    _run(fresh_db_url, "downgrade", "0015_rule_set_attachments")
    assert BREAKING not in _check_text(fresh_db_url, sql)
    assert PUBLISHED in _check_text(fresh_db_url, sql)

    _run(fresh_db_url, "upgrade", "head")
    _run(fresh_db_url, "check", "head")
