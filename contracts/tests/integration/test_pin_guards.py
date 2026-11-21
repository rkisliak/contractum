"""Lifecycle guards for pinned versions and contracts (SUB-06, D-91, D-92, D-95, D-99, D-106).

A version any card actively pins cannot be unpublished, and a contract with any active pin cannot
be soft-deleted. The refusal is one 409 whose ``reasons`` array lists every blocker in a fixed
order (latest, referenced, pinned) and whose type is the first reason's slug. A refused unpublish
writes one ``contract.version_unpublish_blocked`` audit event after its unit rolled back, and the
notice consumer turns it into one notice per pinning card, throttled per card and version.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]
Invariant = Callable[[str], list[dict[str, Any]]]

BLOCKED = "contract.version_unpublish_blocked"
NOTICE = "contract.version.unpublish_blocked"
CONTRACTS = "/api/v1/contracts/contracts"
REMOVAL_REASON = "Retiring this version, please move on"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def action_url(contract: str, semver: str, action: str) -> str:
    return f"{CONTRACTS}/{contract}/versions/{semver}/{action}"


async def unpublish(stack: Any, contract: str, semver: str) -> httpx.Response:
    return await asyncio.wait_for(
        stack.client.post(
            action_url(contract, semver, "unpublish"), headers=bearer(stack.admin_token)
        ),
        10,
    )


async def delete(stack: Any, contract: str) -> httpx.Response:
    return await asyncio.wait_for(
        stack.client.delete(f"{CONTRACTS}/{contract}", headers=bearer(stack.admin_token)), 10
    )


async def remove_pin(stack: Any, contract: str, sub: str) -> httpx.Response:
    return await stack.client.post(
        f"{CONTRACTS}/{contract}/subscriptions/{sub}/remove",
        json={"reason": REMOVAL_REASON},
        headers=bearer(stack.admin_token),
    )


def outbox(sql: Sql, stack: Any, action: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT event_id, aggregate_id, scope_path, before, after FROM outbox "
        "WHERE action = %s ORDER BY id",
        action,
    )


def notices(sql: Sql, stack: Any, card_id: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT * FROM card_notice WHERE card_id = %s AND event_type = %s ORDER BY id",
        card_id,
        NOTICE,
    )


def version_state(sql: Sql, stack: Any, contract: str, semver: str) -> str:
    rows = sql(
        stack.contracts_db_url,
        "SELECT state FROM contract_version WHERE contract_id = %s AND semver = %s",
        contract,
        semver,
    )
    return str(rows[0]["state"])


async def _world(stack: Any) -> dict[str, Any]:
    """orders 2.3.0 (Latest) and 3.0.0 (Preview); card Alpha (Org A) and card bravo (Org B).

    Both pin 3.0.0. ``bravo`` has two email contacts and sorts after ``Alpha`` by lower(name).
    """
    org_a = await stack.chain()
    org_b = await stack.chain()
    contract, versions = await stack.publish_versions(org_a.team, ["2.3.0", "3.0.0"])
    alpha = await stack.create_card(org_a.team, name="Alpha", email_enabled=True)
    bravo = await stack.create_card(
        org_b.team,
        name="bravo",
        contacts=[
            {"name": "lead", "kind": "email", "value": "lead@example.org"},
            {"kind": "email", "value": "ops@example.org"},
        ],
        email_enabled=True,
    )
    pins: dict[str, dict[str, Any]] = {}
    for key, card in (("alpha", alpha), ("bravo", bravo)):
        pinned = await stack.subscribe(card["id"], contract, "3.0.0")
        assert pinned.status_code == 201, pinned.text
        pins[key] = pinned.json()
    return {
        "org_a": org_a,
        "org_b": org_b,
        "contract": contract,
        "versions": versions,
        "alpha": alpha,
        "bravo": bravo,
        "pins": pins,
    }


def _entry(card: dict[str, Any], pin: dict[str, Any], contacts: list[dict[str, Any]]) -> Any:
    return {
        "card_id": card["id"],
        "name": card["name"],
        "contacts": contacts,
        "pins": [
            {
                "subscription_id": pin["id"],
                "version_id": pin["version_id"],
                "semver": "3.0.0",
                "pinned_at": pin["pinned_at"],
                "pinned_by": pin["pinned_by"],
            }
        ],
    }


# --- unpublish of a pinned version -------------------------------------------------------------


async def test_unpublishing_a_pinned_version_is_refused_naming_every_card_and_audited_once(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)

        response = await unpublish(stack, w["contract"], "3.0.0")

        assert response.status_code == 409, response.text
        body = response.json()
        assert body["type"] == "urn:dc:problem:version-pinned"
        assert body["status"] == 409
        assert body["reasons"] == [
            {
                "code": "version-pinned",
                "cards": [
                    _entry(
                        w["alpha"],
                        w["pins"]["alpha"],
                        [],
                    ),
                    _entry(
                        w["bravo"],
                        w["pins"]["bravo"],
                        [
                            {"name": "lead", "kind": "email", "value": "lead@example.org"},
                            {"name": None, "kind": "email", "value": "ops@example.org"},
                        ],
                    ),
                ],
            }
        ]
        assert version_state(sql, stack, w["contract"], "3.0.0") == "published"
        assert outbox(sql, stack, "contract.version_unpublished") == []
        blocked = outbox(sql, stack, BLOCKED)
        assert len(blocked) == 1
        assert blocked[0]["aggregate_id"] == w["contract"]
        assert blocked[0]["after"] == {
            "version_id": w["versions"]["3.0.0"],
            "semver": "3.0.0",
            "notify_card_ids": sorted([w["alpha"]["id"], w["bravo"]["id"]]),
        }
        assert blocked[0]["scope_path"], "the event is scoped to the contract's folder path"
        assert invariant(stack.contracts_db_url) == []


async def test_the_blocked_notice_reaches_each_card_once_and_is_throttled_by_cooldown(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        cards = (w["alpha"]["id"], w["bravo"]["id"])
        assert await stack.pump_notices() == 0  # pins write their own events, no notice yet

        assert (await unpublish(stack, w["contract"], "3.0.0")).status_code == 409
        await stack.pump_notices()
        for card in cards:
            rows = notices(sql, stack, card)
            assert len(rows) == 1
            assert rows[0]["version_id"] is not None
            assert str(rows[0]["version_id"]) == w["versions"]["3.0.0"]

        # A second refusal inside the cooldown is audited again but notifies nobody again.
        assert (await unpublish(stack, w["contract"], "3.0.0")).status_code == 409
        assert len(outbox(sql, stack, BLOCKED)) == 2
        assert await stack.pump_notices() == 0
        for card in cards:
            assert len(notices(sql, stack, card)) == 1

        # Redelivery of any blocked event adds nothing.
        for row in outbox(sql, stack, BLOCKED):
            assert await stack.replay_event(row["event_id"]) == 0
        for card in cards:
            assert len(notices(sql, stack, card)) == 1


async def test_after_the_cooldown_a_later_refusal_notifies_again(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(blocked_notice_cooldown_s=1) as stack:
        w = await _world(stack)
        assert (await unpublish(stack, w["contract"], "3.0.0")).status_code == 409
        await stack.pump_notices()
        await asyncio.sleep(1.1)
        assert (await unpublish(stack, w["contract"], "3.0.0")).status_code == 409
        assert await stack.pump_notices() == 2
        for card in (w["alpha"]["id"], w["bravo"]["id"]):
            assert len(notices(sql, stack, card)) == 2


async def test_a_failure_recording_the_blocked_event_never_changes_the_409(
    make_stack: MakeStack,
    sql: Sql,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    from dc_core.uow import UnitOfWork

    async with make_stack() as stack:
        w = await _world(stack)
        reference = await unpublish(stack, w["contract"], "3.0.0")
        assert reference.status_code == 409

        async def broken(self: Any) -> None:
            raise RuntimeError("secret-broker-detail")

        monkeypatch.setattr(UnitOfWork, "commit", broken)
        capfd.readouterr()
        response = await unpublish(stack, w["contract"], "3.0.0")

        assert response.status_code == 409
        for key in ("type", "title", "status", "detail", "reasons"):
            assert response.json()[key] == reference.json()[key]
        assert len(outbox(sql, stack, BLOCKED)) == 1  # only the first refusal was recorded
        logged = capfd.readouterr()
        text = logged.out + logged.err
        assert "blocked_unpublish_not_recorded" in text
        assert "RuntimeError" in text  # the error class is logged ...
        assert "secret-broker-detail" not in text  # ... never its message
        assert w["contract"] in text


async def test_after_the_owner_removes_every_pin_the_unpublish_succeeds(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        assert (await unpublish(stack, w["contract"], "3.0.0")).status_code == 409

        for key in ("alpha", "bravo"):
            removed = await remove_pin(stack, w["contract"], w["pins"][key]["id"])
            assert removed.status_code == 200, removed.text

        response = await unpublish(stack, w["contract"], "3.0.0")
        assert response.status_code == 200, response.text
        assert response.json()["lifecycle"] == "unpublished"
        assert len(outbox(sql, stack, "contract.version_unpublished")) == 1
        assert len(outbox(sql, stack, BLOCKED)) == 1  # the success wrote no blocked event
        assert invariant(stack.contracts_db_url) == []


async def test_a_pinned_latest_lists_both_reasons_and_takes_the_latest_type(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        pinned = await stack.subscribe(w["alpha"]["id"], w["contract"], "2.3.0")
        assert pinned.status_code == 201, pinned.text

        response = await unpublish(stack, w["contract"], "2.3.0")

        assert response.status_code == 409, response.text
        body = response.json()
        assert body["type"] == "urn:dc:problem:latest-version-protected"
        assert [r["code"] for r in body["reasons"]] == [
            "latest-version-protected",
            "version-pinned",
        ]
        assert body["reasons"][0] == {"code": "latest-version-protected"}
        cards = body["reasons"][1]["cards"]
        assert [c["card_id"] for c in cards] == [w["alpha"]["id"]]
        assert [p["semver"] for p in cards[0]["pins"]] == ["2.3.0"]
        assert version_state(sql, stack, w["contract"], "2.3.0") == "published"


async def test_an_unpinned_preview_unpublishes_as_before(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        org = await stack.chain()
        contract, _ = await stack.publish_versions(org.team, ["1.0.0", "1.1.0"])

        response = await unpublish(stack, contract, "1.1.0")

        assert response.status_code == 200, response.text
        assert response.json()["lifecycle"] == "unpublished"
        assert len(outbox(sql, stack, "contract.version_unpublished")) == 1
        assert outbox(sql, stack, BLOCKED) == []
        assert invariant(stack.contracts_db_url) == []


# --- soft delete of a pinned contract (D-92) ----------------------------------------------------


def _linking_doc(odcs_id: str, target_contract: str) -> bytes:
    """A contract whose authoritativeDefinitions url points at the platform URL of the target."""
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "1.0.0"\n'
        "status: draft\nname: Referrer\n"
        "schema:\n  - name: orders\n    properties:\n      - name: id\n"
        "        logicalType: string\n"
        "authoritativeDefinitions:\n"
        f'  - type: businessDefinition\n    url: "https://dc.example{CONTRACTS}/{target_contract}"\n'
    ).encode()


async def test_deleting_a_pinned_contract_is_refused_naming_every_card_without_notices(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        # alpha also pins the Latest, so its entry lists two pins, highest SemVer first.
        assert (await stack.subscribe(w["alpha"]["id"], w["contract"], "2.3.0")).status_code == 201

        response = await delete(stack, w["contract"])

        assert response.status_code == 409, response.text
        body = response.json()
        assert body["type"] == "urn:dc:problem:contract-pinned"
        assert [r["code"] for r in body["reasons"]] == ["contract-pinned"]
        cards = body["reasons"][0]["cards"]
        assert [c["card_id"] for c in cards] == [w["alpha"]["id"], w["bravo"]["id"]]
        assert [p["semver"] for p in cards[0]["pins"]] == ["3.0.0", "2.3.0"]
        assert [p["semver"] for p in cards[1]["pins"]] == ["3.0.0"]
        assert "referrers" not in body
        still = await stack.client.get(
            f"{CONTRACTS}/{w['contract']}", headers=bearer(stack.admin_token)
        )
        assert still.status_code == 200, still.text
        assert outbox(sql, stack, "contract.deleted") == []
        assert outbox(sql, stack, BLOCKED) == []
        await stack.pump_notices()
        for key in ("alpha", "bravo"):
            assert notices(sql, stack, w[key]["id"]) == []
        assert invariant(stack.contracts_db_url) == []


async def test_a_referenced_and_pinned_contract_lists_both_reasons_and_keeps_the_old_keys(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        referrer, _draft, _etag = await stack.create_contract(
            w["org_a"].team, _linking_doc(f"urn:dc:ref:{w['contract']}", w["contract"])
        )
        published = await stack.publish(referrer, _draft, _etag)
        assert published.status_code == 201, published.text

        response = await delete(stack, w["contract"])

        assert response.status_code == 409, response.text
        body = response.json()
        assert body["type"] == "urn:dc:problem:contract-referenced"
        assert body["referrers"] == [
            {"contract_id": referrer, "odcs_id": f"urn:dc:ref:{w['contract']}"}
        ]
        assert body["hidden_referrer_count"] == 0
        assert [r["code"] for r in body["reasons"]] == ["contract-referenced", "contract-pinned"]
        assert body["reasons"][0]["referrers"] == body["referrers"]
        assert body["reasons"][0]["hidden_referrer_count"] == 0
        assert [c["card_id"] for c in body["reasons"][1]["cards"]] == [
            w["alpha"]["id"],
            w["bravo"]["id"],
        ]


async def test_after_the_pins_are_removed_the_delete_succeeds_as_before(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        w = await _world(stack)
        assert (await delete(stack, w["contract"])).status_code == 409
        for key in ("alpha", "bravo"):
            removed = await remove_pin(stack, w["contract"], w["pins"][key]["id"])
            assert removed.status_code == 200, removed.text

        response = await delete(stack, w["contract"])

        assert response.status_code == 204, response.text
        assert len(outbox(sql, stack, "contract.deleted")) == 1
        assert invariant(stack.contracts_db_url) == []


async def test_an_unpinned_unreferenced_contract_deletes_with_204(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        org = await stack.chain()
        contract, _ = await stack.publish_versions(org.team, ["1.0.0"])
        card = await stack.create_card(org.team)
        pinned = await stack.subscribe(card["id"], contract, "1.0.0")
        assert pinned.status_code == 201, pinned.text
        ended = await stack.client.post(
            f"/api/v1/contracts/cards/{card['id']}/subscriptions/{pinned.json()['id']}/unsubscribe",
            json={},
            headers=bearer(stack.admin_token),
        )
        assert ended.status_code == 200, ended.text

        response = await delete(stack, contract)  # an ended pin never blocks

        assert response.status_code == 204, response.text
