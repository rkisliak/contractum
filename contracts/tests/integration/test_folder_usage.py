"""Folder usage, purge and the whole folder delete across identity, contracts and validation.

Quick 261119-0v1. The stack runs the real identity, validation and contracts apps; identity's
folder-usage port is wired to the other two apps in-process (``HttpFolderUsage`` over ASGI), so a
``DELETE /folders/{id}`` through identity exercises the real internal usage and purge routes, the
immutability-trigger bypass and the audit events.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from dc_identity.folder_usage import HttpFolderUsage

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

CONTRACTS = "/api/v1/contracts/contracts"
FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "dc-odcs" / "tests" / "fixtures"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, *, version: str = "1.0.0") -> bytes:
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        "status: draft\nname: Orders\n"
        "schema:\n  - name: orders\n    properties:\n      - name: id\n"
        "        logicalType: string\n"
    ).encode()


def usage_url(folder: str) -> str:
    return f"/internal/v1/contracts/folders/{folder}/usage"


def purge_url(folder: str) -> str:
    return f"/internal/v1/contracts/folders/{folder}/purge"


def wire_identity(stack: Any) -> None:
    """Point identity's folder-usage port at the in-process contracts and validation apps."""
    contracts = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.contracts_app), base_url="http://contracts"
    )
    validation = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.validation_app), base_url="http://validation"
    )
    stack.identity_app.state.folder_usage = HttpFolderUsage(contracts, validation, timeout_s=10.0)


async def published(stack: Any, team: str, *, version: str = "1.0.0") -> tuple[str, str]:
    odcs_id = f"folderusage-{uuid.uuid4().hex}"
    contract, draft, etag = await stack.create_contract(team, doc(odcs_id, version=version))
    assert (await stack.publish(contract, draft, etag)).status_code == 201
    return contract, odcs_id


async def soft_delete(stack: Any, contract: str) -> None:
    resp = await stack.client.delete(f"{CONTRACTS}/{contract}", headers=bearer(stack.admin_token))
    assert resp.status_code == 204, resp.text


async def create_template(stack: Any, folder: str) -> str:
    resp = await stack.client.post(
        f"/api/v1/contracts/folders/{folder}/templates",
        params={"name": f"tpl-{uuid.uuid4().hex[:6]}"},
        content=(FIXTURES / "valid-v3.2.0.yaml").read_bytes(),
        headers={**bearer(stack.admin_token), "Content-Type": "application/yaml"},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["template"]["id"])


async def archive_card(stack: Any, card: str) -> None:
    resp = await stack.client.post(
        f"/api/v1/contracts/cards/{card}/archive", headers=bearer(stack.admin_token)
    )
    assert resp.status_code == 200, resp.text


def count(stack: Any, sql: Sql, table: str, where: str = "true", *params: object) -> int:
    query = f"SELECT count(*) AS n FROM {table} WHERE {where}"  # noqa: S608 - test-only literals
    rows = sql(stack.contracts_db_url, query, *params)
    return int(rows[0]["n"])


async def test_usage_counts_live_things_as_blockers_and_leftovers_as_purges(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        empty = await stack.client.get(usage_url(chain.team), headers=bearer(stack.admin_token))
        assert empty.status_code == 200, empty.text
        assert empty.json() == {
            "folder_id": chain.team,
            "contracts": 0,
            "templates": 0,
            "subscriber_cards": 0,
            "deleted_contracts": 0,
            "archived_subscriber_cards": 0,
        }

        await published(stack, chain.team)
        gone, _ = await published(stack, chain.team)
        await soft_delete(stack, gone)
        draft_only, _draft, _etag = await stack.create_contract(
            chain.team, doc(f"folderusage-{uuid.uuid4().hex}")
        )
        assert draft_only  # a contract with only a draft is live too
        await create_template(stack, chain.team)
        await stack.create_card(chain.team)
        retired = await stack.create_card(chain.team)
        await archive_card(stack, retired["id"])

        got = await stack.client.get(usage_url(chain.team), headers=bearer(stack.admin_token))
        assert got.status_code == 200, got.text
        assert got.json() == {
            "folder_id": chain.team,
            "contracts": 2,
            "templates": 1,
            "subscriber_cards": 1,
            "deleted_contracts": 1,
            "archived_subscriber_cards": 1,
        }


async def test_usage_counts_contracts_at_organization_project_and_root_folders(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        for folder in (chain.org, chain.project, stack.root_id):
            before = await stack.client.get(usage_url(folder), headers=bearer(stack.admin_token))
            assert before.status_code == 200, before.text
            base = before.json()["contracts"]
            await published(stack, folder)
            gone, _ = await published(stack, folder)
            await soft_delete(stack, gone)
            got = await stack.client.get(usage_url(folder), headers=bearer(stack.admin_token))
            assert got.status_code == 200, got.text
            body = got.json()
            assert body["contracts"] == base + 1, (folder, body)
            assert body["deleted_contracts"] >= 1, (folder, body)


async def test_usage_counts_live_cards_at_organization_and_project_folders(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        for folder in (chain.org, chain.project):
            await stack.create_card(folder)
            retired = await stack.create_card(folder)
            await archive_card(stack, retired["id"])
            got = await stack.client.get(usage_url(folder), headers=bearer(stack.admin_token))
            assert got.status_code == 200, got.text
            body = got.json()
            assert body["subscriber_cards"] == 1, (folder, body)
            assert body["archived_subscriber_cards"] == 1, (folder, body)


async def test_the_internal_routes_need_folder_delete_with_the_forwarded_bearer(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        editor, principal = stack.token("editor")
        stack.grant_sql(principal, "team_editor", chain.team)
        stranger, _ = stack.token("stranger")
        for url, method in ((usage_url(chain.team), "GET"), (purge_url(chain.team), "POST")):
            anonymous = await stack.client.request(method, url)
            assert anonymous.status_code == 401
            # An editor reads the team but lacks folder:delete.
            forbidden = await stack.client.request(method, url, headers=bearer(editor))
            assert forbidden.status_code == 403
            # No grant at all: the folder is invisible.
            unknown = await stack.client.request(method, url, headers=bearer(stranger))
            assert unknown.status_code == 404


async def test_purge_refuses_while_a_blocker_is_left_and_removes_nothing(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        live, _ = await published(stack, chain.team)
        gone, _ = await published(stack, chain.team)
        await soft_delete(stack, gone)
        refused = await stack.client.post(purge_url(chain.team), headers=bearer(stack.admin_token))
        assert refused.status_code == 409, refused.text
        assert refused.json()["type"] == "urn:dc:problem:folder-not-empty"
        assert refused.json()["blockers"] == [{"kind": "contracts", "count": 1}]
        assert count(stack, sql, "contract", "id = %s", gone) == 1
        assert count(stack, sql, "contract", "id = %s", live) == 1


async def test_deleting_a_folder_purges_its_leftovers_and_only_its_leftovers(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        wire_identity(stack)
        chain = await stack.chain()
        other_team = await stack.team_in_org(chain.org, "other")
        team = chain.team
        admin = bearer(stack.admin_token)

        # In the folder: a live contract (blocks first), then soft-deleted contracts with versions
        # and a draft, an archived card whose pin ended, an unused rule set.
        live, _ = await published(stack, team)
        gone, _ = await published(stack, team)
        extra = await stack.client.post(
            f"{CONTRACTS}/{gone}/drafts",
            json={"from_version": "1.0.0", "bump": "minor"},
            headers=admin,
        )
        assert extra.status_code == 201, extra.text
        # Elsewhere: a contract copied from ``gone`` and a card in another folder whose pin on
        # ``gone`` has ended, and a contract the archived card pinned.
        copy = await stack.client.post(
            f"/api/v1/contracts/folders/{other_team}/contracts/from-version",
            json={
                "source_contract_id": gone,
                "source_version": "1.0.0",
                "odcs_id": f"folderusage-copy-{uuid.uuid4().hex}",
            },
            headers=admin,
        )
        assert copy.status_code == 201, copy.text
        copy_id = copy.json()["contract"]["id"]
        kept, _ = await published(stack, other_team)
        outside_card = await stack.create_card(other_team)
        pin = await stack.subscribe(outside_card["id"], gone, "1.0.0")
        assert pin.status_code == 201, pin.text
        unpinned = await stack.client.post(
            f"/api/v1/contracts/cards/{outside_card['id']}/subscriptions/{pin.json()['id']}"
            "/unsubscribe",
            json={},
            headers=admin,
        )
        assert unpinned.status_code == 200, unpinned.text
        await soft_delete(stack, gone)
        gone2, _ = await published(stack, team)
        await soft_delete(stack, gone2)
        card = await stack.create_card(team)
        assert (await stack.subscribe(card["id"], kept, "1.0.0")).status_code == 201
        await archive_card(stack, card["id"])
        pm = stack.policy_manager(team)
        unused = await stack.new_rule_set(
            team, pm, {"unique-property-names": stack.rule_body()}, name="Unused set"
        )

        # Live contract: 409 and nothing is purged.
        refused = await stack.identity.delete(f"/api/v1/identity/folders/{team}", headers=admin)
        assert refused.status_code == 409, refused.text
        assert refused.json()["blockers"] == [{"kind": "contracts", "count": 1}]
        assert count(stack, sql, "contract", "folder_id = %s", team) == 3
        assert sql(stack.validation_db_url, "SELECT count(*) AS n FROM rule_set")[0]["n"] == 1

        # The check lists what would go with the folder once the live contract is deleted.
        await soft_delete(stack, live)
        check = await stack.identity.get(
            f"/api/v1/identity/folders/{team}/deletion-check", headers=admin
        )
        assert check.status_code == 200, check.text
        assert check.json()["deletable"] is True
        assert check.json()["purges"] == [
            {"kind": "deleted_contracts", "count": 3},
            {"kind": "archived_subscriber_cards", "count": 1},
            {"kind": "unused_rule_sets", "count": 1},
        ]
        before_versions = count(stack, sql, "contract_version")

        deleted = await stack.identity.delete(f"/api/v1/identity/folders/{team}", headers=admin)
        assert deleted.status_code == 204, deleted.text

        # Everything of the folder is gone, in the right dependency order, in both services.
        assert count(stack, sql, "contract", "folder_id = %s", team) == 0
        assert count(stack, sql, "subscriber_card", "folder_id = %s", team) == 0
        assert count(stack, sql, "contract_version", "contract_id IN (%s, %s)", live, gone) == 0
        assert count(stack, sql, "card_subscription", "contract_id = %s", gone) == 0
        assert count(stack, sql, "card_subscription", "card_id = %s", card["id"]) == 0
        assert count(stack, sql, "contract_version") < before_versions
        assert sql(stack.validation_db_url, "SELECT count(*) AS n FROM rule_set")[0]["n"] == 0
        assert (
            sql(stack.validation_db_url, "SELECT count(*) AS n FROM rule_set_version")[0]["n"] == 0
        )
        assert unused  # the id existed
        # Other folders keep their rows; the copy lost only its provenance link.
        assert count(stack, sql, "contract", "id = %s", kept) == 1
        assert count(stack, sql, "contract_version", "contract_id = %s", kept) == 1
        copied = sql(
            stack.contracts_db_url,
            "SELECT origin_contract_id, origin_version_id FROM contract WHERE id = %s",
            copy_id,
        )[0]
        assert copied == {"origin_contract_id": None, "origin_version_id": None}
        # The outside card's ended pin on the purged contract was history of that contract.
        assert count(stack, sql, "subscriber_card", "id = %s", outside_card["id"]) == 1
        # The folder itself is gone from identity and one event per service recorded the counts.
        assert (
            await stack.identity.get(f"/api/v1/identity/folders/{team}", headers=admin)
        ).status_code == 404
        contracts_event = sql(
            stack.contracts_db_url,
            "SELECT after FROM outbox WHERE action = 'folder.purged' AND aggregate_id = %s",
            team,
        )
        assert len(contracts_event) == 1
        assert contracts_event[0]["after"]["deleted_contracts"] == 3
        assert contracts_event[0]["after"]["archived_subscriber_cards"] == 1
        assert contracts_event[0]["after"]["origin_cleared"] == 1
        validation_event = sql(
            stack.validation_db_url,
            "SELECT after FROM outbox WHERE action = 'folder.purged' AND aggregate_id = %s",
            team,
        )
        assert len(validation_event) == 1
        assert validation_event[0]["after"]["rule_sets"] == 1
        identity_event = sql(
            stack.identity_db_url,
            "SELECT before FROM outbox WHERE action = 'folder.deleted' AND aggregate_id = %s",
            team,
        )
        assert len(identity_event) == 1
        assert identity_event[0]["before"]["purged"]["deleted_contracts"] == 3

        # Creating anything in the deleted folder now fails: it no longer resolves.
        again = await stack.post(team, doc(f"folderusage-{uuid.uuid4().hex}"))
        assert again.status_code == 404


async def test_a_mandatory_rule_set_blocks_the_delete_and_nothing_is_purged(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        wire_identity(stack)
        chain = await stack.chain()
        gone, _ = await published(stack, chain.team)
        await soft_delete(stack, gone)
        await stack.mandatory_rule_set(chain.team, enforce=False)
        refused = await stack.identity.delete(
            f"/api/v1/identity/folders/{chain.team}", headers=bearer(stack.admin_token)
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["blockers"] == [{"kind": "rule_sets", "count": 1}]
        # Blocked before the purge: the soft-deleted contract is still there.
        assert count(stack, sql, "contract", "id = %s", gone) == 1
        assert sql(stack.validation_db_url, "SELECT count(*) AS n FROM rule_set")[0]["n"] == 1


async def test_an_outage_of_validation_is_503_and_the_folder_stays(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        wire_identity(stack)
        chain = await stack.chain()
        gone, _ = await published(stack, chain.team)
        await soft_delete(stack, gone)
        stack.identity_app.state.folder_usage = HttpFolderUsage(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=stack.contracts_app), base_url="http://contracts"
            ),
            httpx.AsyncClient(transport=httpx.MockTransport(_refuse), base_url="http://validation"),
            timeout_s=5.0,
        )
        resp = await stack.identity.delete(
            f"/api/v1/identity/folders/{chain.team}", headers=bearer(stack.admin_token)
        )
        assert resp.status_code == 503, resp.text
        assert count(stack, sql, "contract", "id = %s", gone) == 1
        assert (
            await stack.identity.get(
                f"/api/v1/identity/folders/{chain.team}", headers=bearer(stack.admin_token)
            )
        ).status_code == 200


def _refuse(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500)


async def test_the_purge_bypass_never_reaches_live_contracts_or_active_pins(
    make_stack: MakeStack, sql: Sql
) -> None:
    """With the purge setting on, the triggers still refuse live rows and active pins."""
    async with make_stack() as stack:
        chain = await stack.chain()
        live, _ = await published(stack, chain.team)
        card = await stack.create_card(chain.team)
        pin = await stack.subscribe(card["id"], live, "1.0.0")
        assert pin.status_code == 201, pin.text
        gone, _ = await published(stack, chain.team)
        await soft_delete(stack, gone)
        url = stack.contracts_db_url.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(url, autocommit=True) as conn:
            # Without the setting even a deleted contract's versions stay.
            with pytest.raises(psycopg.errors.RaiseException):
                conn.execute("DELETE FROM contract_version WHERE contract_id = %s", (gone,))
            for statement, params in (
                ("DELETE FROM contract_version WHERE contract_id = %s", (live,)),
                ("DELETE FROM card_subscription WHERE id = %s", (pin.json()["id"],)),
            ):
                with conn.transaction():
                    conn.execute("SELECT set_config('dc.folder_purge', 'on', true)")
                    with pytest.raises(psycopg.errors.RaiseException):
                        conn.execute(statement, params)  # type: ignore[arg-type]
        assert count(stack, sql, "contract_version", "contract_id = %s", live) == 1
        assert count(stack, sql, "card_subscription", "id = %s", pin.json()["id"]) == 1
