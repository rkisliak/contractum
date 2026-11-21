"""Validation authorises the stored-run read itself (D-155, RESEARCH Pattern 8, T-6-26).

``GET /internal/v1/validation/runs/{run_id}?contract_id=`` is how the contracts report fetch reads
the run a version was published with. Internal routes are not routed by the gateway, but any
principal inside the network could call validation directly, so the route checks
``contract:read-published`` at the run's stored folder with the caller's own token, requires the
stored contract id to equal the one sent and serves publishable runs only. Everything else is the
same uniform 404, so a run id is never an IDOR.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def contract_yaml(*names: str) -> bytes:
    document = {
        "kind": "DataContract",
        "apiVersion": "v3.2.0",
        "id": f"urn:datacontract:run-read:{uuid.uuid4().hex}",
        "version": "1.0.0",
        "status": "draft",
        "schema": [
            {
                "name": "orders",
                "properties": [{"name": n, "logicalType": "string"} for n in names],
            }
        ],
    }
    return yaml.safe_dump(document, sort_keys=False).encode()


async def publish_run(
    stack: Any,
    folder_id: str,
    token: str,
    *,
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Store a publish-mode run at ``folder_id``; returns (run id, contract id, answer)."""
    contract_id = str(uuid.uuid4())
    payload = {
        "mode": "publish",
        "contract_id": contract_id,
        "draft_id": str(uuid.uuid4()),
        "media_type": "application/yaml",
        "document_b64": base64.b64encode(contract_yaml("id", "amount")).decode(),
        "attachments": attachments or [],
        "attachments_revision": 0,
    }
    response = await stack.client.post(
        f"/internal/v1/validation/folders/{folder_id}/runs",
        content=json.dumps(payload),
        headers={**auth(token), "Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] is not None
    return str(body["run_id"]), contract_id, body


async def read_run(stack: Any, run_id: str, contract_id: str, token: str | None) -> Any:
    headers = auth(token) if token else {}
    return await stack.client.get(
        f"/internal/v1/validation/runs/{run_id}",
        params={"contract_id": contract_id},
        headers=headers,
    )


def stable(response: Any) -> tuple[int, list[tuple[str, Any]]]:
    body = dict(response.json())
    for volatile in ("request_id", "instance"):
        body.pop(volatile, None)
    return response.status_code, sorted(body.items())


async def test_the_right_caller_reads_the_stored_publish_run_with_its_report_and_pins(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        editor, editor_id = stack.token("ed")
        stack.grant_sql(editor_id, "team_editor", chain.team)
        consumer, consumer_id = stack.token("cs")
        stack.grant_sql(consumer_id, "consumer", chain.team)
        run_id, contract_id, answer = await publish_run(stack, chain.team, editor)

        for token in (editor, consumer, stack.admin_token):
            response = await read_run(stack, run_id, contract_id, token)

            assert response.status_code == 200, response.text
            body = response.json()
            assert body["run_id"] == run_id
            assert body["contract_id"] == contract_id
            assert body["content_sha256"] == answer["content_sha256"]
            assert body["report"] == answer["report"]
            assert body["pins"] == answer["pins"]
            assert body["report"]["summary"]["publishable"] is True
            assert body["report"]["compatibility"]["status"] == "no-baseline"


async def test_another_contract_id_another_reader_or_an_unknown_run_is_the_same_404(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        editor, editor_id = stack.token("ed")
        stack.grant_sql(editor_id, "team_editor", chain.team)
        run_id, contract_id, _ = await publish_run(stack, chain.team, editor)

        # A Policy Manager holds no contract:read-published (D-36); an editor of another
        # Organization's team cannot read the stored folder at all.
        manager, manager_id = stack.token("pm")
        stack.grant_sql(manager_id, "policy_manager", chain.org)
        other = await stack.chain()
        stranger, stranger_id = stack.token("other-ed")
        stack.grant_sql(stranger_id, "team_editor", other.team)

        reference = await read_run(stack, str(uuid.uuid4()), contract_id, editor)
        assert reference.status_code == 404, reference.text
        assert reference.json()["type"] == "urn:dc:problem:not-found"
        denied = [
            await read_run(stack, run_id, str(uuid.uuid4()), editor),
            await read_run(stack, run_id, contract_id, manager),
            await read_run(stack, run_id, contract_id, stranger),
        ]
        for response in denied:
            assert stable(response) == stable(reference)
        assert "report" not in json.dumps([r.json() for r in denied])

        anonymous = await read_run(stack, run_id, contract_id, None)
        assert anonymous.status_code == 401


async def test_a_stored_run_that_was_not_publishable_is_never_served(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        editor, editor_id = stack.token("ed")
        stack.grant_sql(editor_id, "team_editor", chain.team)
        # An attachment that cannot resolve is one blocking finding: the refused publish attempt is
        # stored, but its draft never became a version, so nobody reads its report through here.
        run_id, contract_id, answer = await publish_run(
            stack,
            chain.team,
            editor,
            attachments=[{"rule_set_id": str(uuid.uuid4()), "version": "latest"}],
        )
        assert answer["report"]["summary"]["publishable"] is False

        for token in (editor, stack.admin_token):
            response = await read_run(stack, run_id, contract_id, token)
            assert response.status_code == 404, response.text
            assert response.json()["type"] == "urn:dc:problem:not-found"
