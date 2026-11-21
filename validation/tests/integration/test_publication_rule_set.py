"""The seeded platform rule set odcs-publication in real runs (quick 261119-rpi; UD-1).

``published-status-active`` warns when a version is published while the ODCS ``status`` is not
``active``. It binds the cel-2 ``ctx.operation``, so only publish-mode runs report it: draft saves
and the on-demand validation (save mode) never do. It is warning severity, so it never blocks
(D-124), in warn grace and after enforcement alike. The tests run against the stack's migrated
database with the seed kept (``platform_seed=True``).
"""

from __future__ import annotations

import base64
import importlib
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
import yaml
from dc_rules.model import content_sha256
from dc_validation.seed import (
    ODCS_PUBLICATION_NAME,
    ODCS_PUBLICATION_V1,
    PLATFORM_ODCS_PUBLICATION_ID,
    PLATFORM_PUBLICATION_SEED_EVENT_ID,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

BASE = "/api/v1/validation"
RULE_KEY = "published-status-active"
SET_ID = str(PLATFORM_ODCS_PUBLICATION_ID)
MESSAGE = ODCS_PUBLICATION_V1.rules[0].message


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def contract(**extra: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "kind": "DataContract",
        "apiVersion": "v3.2.0",
        "id": "urn:datacontract:test:orders",
        "version": "1.0.0",
        "schema": [{"name": "orders", "properties": [{"name": "id", "logicalType": "string"}]}],
    }
    document.update(extra)
    return document


def dumped(document: dict[str, Any]) -> bytes:
    return yaml.safe_dump(document, sort_keys=False).encode()


async def post_run(
    stack: Any, folder: str, token: str, document: dict[str, Any], mode: str = "publish"
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "mode": mode,
        "media_type": "application/yaml",
        "document_b64": base64.b64encode(dumped(document)).decode(),
        "attachments": [],
        "attachments_revision": 0,
    }
    if mode == "publish":
        body["contract_id"] = str(uuid.uuid4())
        body["draft_id"] = str(uuid.uuid4())
    response = await stack.client.post(
        f"/internal/v1/validation/folders/{folder}/runs", json=body, headers=auth(token)
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def team_editor(stack: Any) -> tuple[Any, str]:
    chain = await stack.chain()
    token, principal = stack.token("ed")
    stack.grant_sql(principal, "team_editor", chain.team)
    return chain, token


def publication(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in body["report"]["findings"] if f["rule_set"]["name"] == ODCS_PUBLICATION_NAME]


@pytest.mark.parametrize(
    "extra",
    [
        {"status": "draft"},
        {"status": "proposed"},
        {"status": "deprecated"},
        {},
        {"status": 1},
        {"status": None},
        {"status": ["active"]},
    ],
    ids=["draft", "proposed", "deprecated", "absent", "integer", "null", "list"],
)
async def test_a_publish_run_of_a_document_whose_status_is_not_active_warns_and_never_blocks(
    make_stack: Any, extra: dict[str, Any]
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)

        body = await post_run(stack, chain.team, editor, contract(**extra))

        (finding,) = publication(body)
        assert finding["rule_key"] == RULE_KEY
        assert finding["outcome"] == "fail"
        assert finding["severity"] == "warning"
        assert finding["source"] == "mandatory"
        assert finding["mode"] == "warn"
        assert finding["blocking"] is False
        assert finding["rule_set"]["scope"] == "platform"
        assert finding["message"] == MESSAGE  # static author text, never a document value
        assert finding["location"]["pointer"] == ""
        # A non-string status is also a JSON Schema error of its own, which does block (so the
        # run is not publishable); the publication finding itself never blocks.
        blocking = [f for f in body["report"]["findings"] if f["blocking"]]
        assert all(f["rule_set"]["name"] != ODCS_PUBLICATION_NAME for f in blocking)
        if "status" not in extra or isinstance(extra["status"], str):
            assert blocking == []
            assert body["report"]["summary"]["publishable"] is True
        pin = next(p for p in body["pins"] if p["name"] == ODCS_PUBLICATION_NAME)
        assert (pin["version"], pin["mode"]) == (1, "warn")


async def test_a_publish_run_of_an_active_document_has_no_publication_finding(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)

        body = await post_run(stack, chain.team, editor, contract(status="active"))

        assert publication(body) == []
        assert body["report"]["summary"]["publishable"] is True


async def test_a_save_run_never_reports_the_publication_finding_whatever_the_status(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)

        for extra in ({"status": "draft"}, {}, {"status": 1}, {"status": "active"}):
            body = await post_run(stack, chain.team, editor, contract(**extra), mode="save")
            assert publication(body) == [], extra
            pin = next(p for p in body["pins"] if p["name"] == ODCS_PUBLICATION_NAME)
            assert pin["version"] == 1


async def test_the_public_on_demand_validate_route_never_reports_it(make_stack: Any) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)

        response = await stack.client.post(
            f"{BASE}/folders/{chain.team}/validate",
            content=dumped(contract(status="draft")),
            headers={**auth(editor), "Content-Type": "application/yaml"},
        )

        assert response.status_code == 200, response.text
        assert publication(response.json()) == []


async def test_the_warning_never_blocks_after_enforce_now_or_a_past_enforce_at(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)
        document = contract(status="draft")
        enforce = (
            f"{BASE}/folders/{stack.root_id}/platform-rule-sets/{SET_ID}/versions/1/enforce-now"
        )

        response = await stack.client.post(enforce, headers=auth(stack.admin_token))
        assert response.status_code == 200, response.text
        enforced = await post_run(stack, chain.team, editor, document)

        (finding,) = publication(enforced)
        assert (finding["mode"], finding["severity"], finding["blocking"]) == (
            "enforce",
            "warning",
            False,
        )
        assert enforced["report"]["summary"]["publishable"] is True

        # A past enforce-at is the same state as enforce-now.
        stack.validation_sql(
            "UPDATE rule_set_version SET enforce_at = now() - interval '1 day'"
            " WHERE rule_set_id = %s",
            SET_ID,
        )
        later = await post_run(stack, chain.team, editor, document)
        (finding,) = publication(later)
        assert (finding["mode"], finding["blocking"]) == ("enforce", False)
        assert later["report"]["summary"]["publishable"] is True

        # Pause and resume work through the existing ROOT route; paused stays non-blocking warn.
        paused = await stack.client.put(
            f"{BASE}/folders/{stack.root_id}/platform-rule-sets/{SET_ID}/paused",
            json={"paused": True},
            headers=auth(stack.admin_token),
        )
        assert paused.status_code == 200, paused.text
        (finding,) = publication(await post_run(stack, chain.team, editor, document))
        assert (finding["mode"], finding["blocking"]) == ("warn", False)


async def test_the_root_available_rule_sets_list_the_publication_set(make_stack: Any) -> None:
    async with make_stack(platform_seed=True) as stack:
        response = await stack.get(
            f"{BASE}/folders/{stack.root_id}/available-rule-sets", token=stack.admin_token
        )

        assert response.status_code == 200, response.text
        item = next(i for i in response.json()["items"] if i["name"] == ODCS_PUBLICATION_NAME)
        assert item["id"] == SET_ID
        assert item["latest_version"] == 1
        assert item["mandatory"] is True
        assert [(r["version_no"], r["mode"]) for r in item["resolved"]] == [(1, "warn")]


def publication_migration() -> Any:
    return importlib.import_module(
        "dc_validation.migrations.versions.0009_seed_publication_rule_set"
    )


async def test_the_seed_is_stored_with_the_constant_hash_and_is_idempotent(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        engine = sa.create_engine(stack.validation_db_url)
        try:
            for _ in range(2):
                with engine.begin() as connection:
                    for statement in publication_migration().seed_statements():
                        connection.execute(statement)
        finally:
            engine.dispose()

        sets = stack.validation_sql("SELECT * FROM rule_set WHERE name = %s", ODCS_PUBLICATION_NAME)
        assert len(sets) == 1
        assert sets[0]["scope"] == "platform"
        assert sets[0]["mandatory"] is True
        versions = stack.validation_sql(
            "SELECT * FROM rule_set_version WHERE rule_set_id = %s", SET_ID
        )
        assert len(versions) == 1
        assert versions[0]["dsl_version"] == "cel-2"
        assert versions[0]["rule_keys"] == [RULE_KEY]
        assert versions[0]["content_sha256"] == content_sha256(ODCS_PUBLICATION_V1)
        events = stack.validation_sql(
            "SELECT * FROM outbox WHERE event_id = %s", PLATFORM_PUBLICATION_SEED_EVENT_ID
        )
        assert len(events) == 1
