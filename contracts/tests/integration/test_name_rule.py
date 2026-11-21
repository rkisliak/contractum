"""The one name rule (quick 261120-lbz, D-01) on the contracts create and rename routes.

A name with a bidirectional override or an emoji is a 422 ``validation-error`` naming the field
and the code point, never the character. A legacy stored name never blocks an unrelated write.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "dc-odcs" / "tests" / "fixtures"
CONTRACTS = "/api/v1/contracts/contracts"
TEMPLATES = "/api/v1/contracts/templates"
CARDS = "/api/v1/contracts/cards"

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

SPOOF = "Team\u202eevil"
EMOJI = "Orders \U0001f600"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, name: str) -> bytes:
    # A JSON string literal is a valid YAML double-quoted scalar, so escapes stay ASCII.
    import json

    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "1.0.0"\n'
        f"status: draft\nname: {json.dumps(name, ensure_ascii=False)}\n"
        "schema:\n  - name: orders\n    properties:\n      - name: id\n"
        "        logicalType: string\n"
    ).encode()


def refused(resp: Any, pointer: str, point: str) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["type"] == "urn:dc:problem:validation-error"
    assert body["errors"][0]["pointer"] == pointer
    assert point in body["errors"][0]["detail"]
    assert "\u202e" not in resp.text
    assert "\U0001f600" not in resp.text


def new_id() -> str:
    return f"urn:dc:names:{uuid.uuid4().hex}"


async def test_starter_and_uploaded_document_names_are_checked(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        url = f"/api/v1/contracts/folders/{chain.team}/contracts/starter"
        starter = await stack.client.post(
            url, json={"odcs_id": new_id(), "name": SPOOF}, headers=bearer(stack.admin_token)
        )
        refused(starter, "/body/name", "U+202E")
        ok = await stack.client.post(
            url,
            json={"odcs_id": new_id(), "name": "  数据 Orders  "},
            headers=bearer(stack.admin_token),
        )
        assert ok.status_code == 201, ok.text
        assert ok.json()["contract"]["name"] == "数据 Orders"

        before = sql(stack.contracts_db_url, "SELECT count(*) AS n FROM contract")[0]["n"]
        upload = await stack.post(chain.team, doc(new_id(), EMOJI))
        refused(upload, "/name", "U+1F600")
        spoofed = await stack.post(chain.team, doc(new_id(), SPOOF))
        refused(spoofed, "/name", "U+202E")
        assert sql(stack.contracts_db_url, "SELECT count(*) AS n FROM contract")[0]["n"] == before
        valid = await stack.post(chain.team, doc(new_id(), "Équipe (EU) & Co."))
        assert valid.status_code == 201, valid.text


async def test_a_draft_save_checks_only_a_changed_name(make_stack: MakeStack, sql: Sql) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_id()
        created = await stack.post(chain.team, doc(odcs_id, "Orders"))
        assert created.status_code == 201, created.text
        contract, draft = created.json()["contract"]["id"], created.json()["draft"]["id"]
        # A legacy name stored before the rule existed.
        sql(
            stack.contracts_db_url,
            "UPDATE contract_version SET name = %s WHERE id = %s",
            SPOOF,
            uuid.UUID(draft),
        )
        url = f"{CONTRACTS}/{contract}/drafts/{draft}"
        headers = {**bearer(stack.admin_token), "Content-Type": "application/yaml"}

        kept = await stack.client.put(
            url, content=doc(odcs_id, SPOOF), headers={**headers, "If-Match": '"r1"'}
        )
        assert kept.status_code == 200, kept.text
        changed = await stack.client.put(
            url,
            content=doc(odcs_id, "Team\u202dother"),
            headers={**headers, "If-Match": '"r2"'},
        )
        refused(changed, "/name", "U+202D")
        fixed = await stack.client.put(
            url, content=doc(odcs_id, "Orders v2"), headers={**headers, "If-Match": '"r2"'}
        )
        assert fixed.status_code == 200, fixed.text


async def test_template_names_and_a_template_with_a_bad_document_name(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        headers = {**bearer(stack.admin_token), "Content-Type": "application/yaml"}
        body = (FIXTURES / "valid-v3.2.0.yaml").read_bytes()
        create_url = f"/api/v1/contracts/folders/{chain.org}/templates"
        bad = await stack.client.post(
            create_url, params={"name": SPOOF}, content=body, headers=headers
        )
        refused(bad, "/query/name", "U+202E")
        ok = await stack.client.post(
            create_url, params={"name": "  Orders template  "}, content=body, headers=headers
        )
        assert ok.status_code == 201, ok.text
        template = ok.json()["template"]
        assert template["name"] == "Orders template"

        draft_url = f"{TEMPLATES}/{template['id']}/draft"
        bad_put = await stack.client.put(
            draft_url, params={"name": EMOJI}, content=body, headers=headers
        )
        refused(bad_put, "/query/name", "U+1F600")
        # A save without a name is unaffected.
        plain = await stack.client.put(draft_url, content=body, headers=headers)
        assert plain.status_code == 200, plain.text

        # A template whose document carries a bad name publishes, but a contract made from it
        # is a new write and is refused at the document's name.
        evil = await stack.client.post(
            create_url,
            params={"name": "Evil doc"},
            content=doc(new_id(), SPOOF),
            headers=headers,
        )
        assert evil.status_code == 201, evil.text
        evil_id = evil.json()["template"]["id"]
        published = await stack.client.post(
            f"{TEMPLATES}/{evil_id}/publish", headers=bearer(stack.admin_token)
        )
        assert published.status_code == 201, published.text
        made = await stack.client.post(
            f"/api/v1/contracts/folders/{chain.team}/contracts/from-template",
            json={"template_id": evil_id, "odcs_id": new_id()},
            headers=bearer(stack.admin_token),
        )
        refused(made, "/name", "U+202E")


async def test_card_names_are_checked_and_a_patch_without_a_name_is_unaffected(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        url = f"/api/v1/contracts/folders/{chain.team}/cards"
        bad = await stack.client.post(
            url,
            json={"name": SPOOF, "description": "d"},
            headers=bearer(stack.admin_token),
        )
        refused(bad, "/body/name", "U+202E")
        card = await stack.create_card(chain.team, name="  Ingest card  ")
        assert card["name"] == "Ingest card"
        # A legacy stored name, then a PATCH that leaves the name alone.
        sql(
            stack.contracts_db_url,
            "UPDATE subscriber_card SET name = %s WHERE id = %s",
            SPOOF,
            uuid.UUID(card["id"]),
        )
        patch = await stack.client.patch(
            f"{CARDS}/{card['id']}",
            json={"description": "Nightly"},
            headers=bearer(stack.admin_token),
        )
        assert patch.status_code == 200, patch.text
        rename = await stack.client.patch(
            f"{CARDS}/{card['id']}", json={"name": EMOJI}, headers=bearer(stack.admin_token)
        )
        refused(rename, "/body/name", "U+1F600")
