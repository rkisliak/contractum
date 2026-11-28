"""The seeded platform rule set odcs-publication in real runs (quick 261119-rpi, 261128-rsa).

Version 1 (migration 0009) holds the warning ``published-status-active``. Version 2 (migration
0010) adds the error ``released-status-active``: a released version (no prerelease part) published
with an ODCS ``status`` other than ``active`` is refused once version 2 is enforced, while a Preview
published with a prerelease version keeps the warning. Both versions start in warn grace, so before
``enforce_at`` nothing blocks. The rules bind the cel-2 ``ctx.operation``, so only publish-mode runs
report them: draft saves and the on-demand validation (save mode) never do. The tests run against
the stack's migrated database with the seed kept (``platform_seed=True``).
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
    ODCS_PUBLICATION_V2,
    PLATFORM_ODCS_PUBLICATION_ID,
    PLATFORM_ODCS_PUBLICATION_V2_ID,
    PLATFORM_PUBLICATION_SEED_EVENT_ID,
    PLATFORM_PUBLICATION_V2_EVENT_ID,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

BASE = "/api/v1/validation"
RULE_KEY = "published-status-active"
RELEASED_KEY = "released-status-active"
SET_ID = str(PLATFORM_ODCS_PUBLICATION_ID)
MESSAGE = ODCS_PUBLICATION_V1.rules[0].message
V2_MESSAGES = {rule.key: rule.message for rule in ODCS_PUBLICATION_V2.rules}
ENFORCE_NOW = "/platform-rule-sets/{set_id}/versions/{n}/enforce-now"


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


def by_version(body: dict[str, Any]) -> dict[int, list[str]]:
    """Publication findings as ``{rule-set version: [rule keys]}``."""
    found: dict[int, list[str]] = {}
    for finding in publication(body):
        found.setdefault(finding["rule_set"]["version"], []).append(finding["rule_key"])
    return found


def pins(body: dict[str, Any]) -> list[tuple[int, str]]:
    return [(p["version"], p["mode"]) for p in body["pins"] if p["name"] == ODCS_PUBLICATION_NAME]


async def enforce_now(stack: Any, version: int) -> None:
    path = ENFORCE_NOW.format(set_id=SET_ID, n=version)
    response = await stack.client.post(
        f"{BASE}/folders/{stack.root_id}{path}", headers=auth(stack.admin_token)
    )
    assert response.status_code == 200, response.text


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
async def test_in_grace_a_released_version_without_status_active_is_reported_never_blocked(
    make_stack: Any, extra: dict[str, Any]
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)

        body = await post_run(stack, chain.team, editor, contract(**extra))

        assert by_version(body) == {1: [RULE_KEY], 2: [RELEASED_KEY]}
        v1, v2 = sorted(publication(body), key=lambda f: f["rule_set"]["version"])
        assert (v1["severity"], v1["mode"], v1["blocking"]) == ("warning", "warn", False)
        assert v1["message"] == MESSAGE  # static author text, never a document value
        assert (v2["severity"], v2["mode"], v2["blocking"]) == ("error", "warn", False)
        assert v2["message"] == V2_MESSAGES[RELEASED_KEY]
        for finding in (v1, v2):
            assert finding["outcome"] == "fail"
            assert finding["source"] == "mandatory"
            assert finding["rule_set"]["scope"] == "platform"
            assert finding["location"]["pointer"] == ""
        # A non-string status is also a JSON Schema error of its own, which does block (so the
        # run is not publishable); in grace the publication findings themselves never block.
        blocking = [f for f in body["report"]["findings"] if f["blocking"]]
        assert all(f["rule_set"]["name"] != ODCS_PUBLICATION_NAME for f in blocking)
        if "status" not in extra or isinstance(extra["status"], str):
            assert blocking == []
            assert body["report"]["summary"]["publishable"] is True
        assert pins(body) == [(1, "warn"), (2, "warn")]


async def test_a_prerelease_without_status_active_gets_the_warning_only(make_stack: Any) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)

        for extra in ({"status": "draft"}, {"status": "proposed"}, {}):
            document = contract(version="1.1.0-rc.1", **extra)
            body = await post_run(stack, chain.team, editor, document)

            assert by_version(body) == {1: [RULE_KEY], 2: [RULE_KEY]}, extra
            assert all(f["severity"] == "warning" for f in publication(body))
            assert body["report"]["summary"]["publishable"] is True


async def test_a_publish_run_of_an_active_document_has_no_publication_finding(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)

        for version in ("1.0.0", "1.1.0-rc.1"):
            body = await post_run(
                stack, chain.team, editor, contract(version=version, status="active")
            )

            assert publication(body) == [], version
            assert body["report"]["summary"]["publishable"] is True


async def test_a_save_run_never_reports_the_publication_findings_whatever_the_status(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)

        for extra in ({"status": "draft"}, {}, {"status": 1}, {"status": "active"}):
            body = await post_run(stack, chain.team, editor, contract(**extra), mode="save")
            assert publication(body) == [], extra
            assert pins(body) == [(1, "warn"), (2, "warn")]


async def test_the_public_on_demand_validate_route_never_reports_them(make_stack: Any) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)
        await enforce_now(stack, 2)

        response = await stack.client.post(
            f"{BASE}/folders/{chain.team}/validate",
            content=dumped(contract(status="draft")),
            headers={**auth(editor), "Content-Type": "application/yaml"},
        )

        assert response.status_code == 200, response.text
        assert publication(response.json()) == []


async def test_after_version_2_is_enforced_a_released_version_without_status_active_is_blocked(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)

        await enforce_now(stack, 2)
        refused = await post_run(stack, chain.team, editor, contract(status="draft"))

        # Version 1 is superseded once version 2 is enforced: only version 2 reports.
        assert by_version(refused) == {2: [RELEASED_KEY]}
        (finding,) = publication(refused)
        assert (finding["severity"], finding["mode"], finding["blocking"]) == (
            "error",
            "enforce",
            True,
        )
        assert refused["report"]["summary"]["publishable"] is False
        assert pins(refused) == [(2, "enforce")]

        missing = await post_run(stack, chain.team, editor, contract())
        assert by_version(missing) == {2: [RELEASED_KEY]}
        assert missing["report"]["summary"]["publishable"] is False

        preview = await post_run(
            stack, chain.team, editor, contract(version="1.1.0-rc.1", status="draft")
        )
        assert by_version(preview) == {2: [RULE_KEY]}
        (warning,) = publication(preview)
        assert (warning["severity"], warning["mode"], warning["blocking"]) == (
            "warning",
            "enforce",
            False,
        )
        assert preview["report"]["summary"]["publishable"] is True

        active = await post_run(stack, chain.team, editor, contract(status="active"))
        assert publication(active) == []
        assert active["report"]["summary"]["publishable"] is True

        # A past enforce-at is the same state as enforce-now.
        stack.validation_sql(
            "UPDATE rule_set_version SET enforce_at = now() - interval '1 day'"
            " WHERE rule_set_id = %s",
            SET_ID,
        )
        later = await post_run(stack, chain.team, editor, contract(status="draft"))
        assert later["report"]["summary"]["publishable"] is False

        # A ROOT pause turns the set into non-blocking warn entries again.
        paused = await stack.client.put(
            f"{BASE}/folders/{stack.root_id}/platform-rule-sets/{SET_ID}/paused",
            json={"paused": True},
            headers=auth(stack.admin_token),
        )
        assert paused.status_code == 200, paused.text
        relaxed = await post_run(stack, chain.team, editor, contract(status="draft"))
        (finding,) = publication(relaxed)
        assert (finding["rule_key"], finding["mode"], finding["blocking"]) == (
            RELEASED_KEY,
            "warn",
            False,
        )
        assert relaxed["report"]["summary"]["publishable"] is True


async def test_enforcing_version_1_alone_keeps_its_warning_non_blocking(make_stack: Any) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)

        await enforce_now(stack, 1)
        body = await post_run(stack, chain.team, editor, contract(status="draft"))

        found = {f["rule_set"]["version"]: f for f in publication(body)}
        assert (found[1]["rule_key"], found[1]["mode"], found[1]["blocking"]) == (
            RULE_KEY,
            "enforce",
            False,
        )
        assert (found[2]["rule_key"], found[2]["mode"], found[2]["blocking"]) == (
            RELEASED_KEY,
            "warn",
            False,
        )
        assert body["report"]["summary"]["publishable"] is True


async def test_the_root_available_rule_sets_list_the_publication_set(make_stack: Any) -> None:
    async with make_stack(platform_seed=True) as stack:
        response = await stack.get(
            f"{BASE}/folders/{stack.root_id}/available-rule-sets", token=stack.admin_token
        )

        assert response.status_code == 200, response.text
        item = next(i for i in response.json()["items"] if i["name"] == ODCS_PUBLICATION_NAME)
        assert item["id"] == SET_ID
        assert item["latest_version"] == 2
        assert item["mandatory"] is True
        assert [(r["version_no"], r["mode"]) for r in item["resolved"]] == [
            (1, "warn"),
            (2, "warn"),
        ]


def migration(name: str) -> Any:
    return importlib.import_module(f"dc_validation.migrations.versions.{name}")


async def test_the_seeds_are_stored_with_the_constant_hashes_and_are_idempotent(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        engine = sa.create_engine(stack.validation_db_url)
        try:
            for _ in range(2):
                with engine.begin() as connection:
                    for name in ("0009_seed_publication_rule_set", "0010_publication_rule_set_v2"):
                        for statement in migration(name).seed_statements():
                            connection.execute(statement)
        finally:
            engine.dispose()

        sets = stack.validation_sql("SELECT * FROM rule_set WHERE name = %s", ODCS_PUBLICATION_NAME)
        assert len(sets) == 1
        assert sets[0]["scope"] == "platform"
        assert sets[0]["mandatory"] is True
        assert sets[0]["publish_counter"] == 2
        assert sets[0]["draft_content_sha256"] == content_sha256(ODCS_PUBLICATION_V2)
        versions = stack.validation_sql(
            "SELECT * FROM rule_set_version WHERE rule_set_id = %s ORDER BY version_no", SET_ID
        )
        assert [(v["version_no"], v["dsl_version"], v["rule_keys"]) for v in versions] == [
            (1, "cel-2", [RULE_KEY]),
            (2, "cel-2", [RULE_KEY, RELEASED_KEY]),
        ]
        assert versions[0]["content_sha256"] == content_sha256(ODCS_PUBLICATION_V1)
        assert versions[1]["content_sha256"] == content_sha256(ODCS_PUBLICATION_V2)
        assert str(versions[1]["id"]) == str(PLATFORM_ODCS_PUBLICATION_V2_ID)
        # Version 2 starts its own warn grace (DC_MANDATORY_WARN_DAYS, 14 by default).
        (grace,) = stack.validation_sql(
            "SELECT enforce_at > now() + interval '13 days' AS later FROM rule_set_version"
            " WHERE id = %s",
            PLATFORM_ODCS_PUBLICATION_V2_ID,
        )
        assert grace["later"] is True
        for event_id, aggregate_version in (
            (PLATFORM_PUBLICATION_SEED_EVENT_ID, 1),
            (PLATFORM_PUBLICATION_V2_EVENT_ID, 2),
        ):
            events = stack.validation_sql("SELECT * FROM outbox WHERE event_id = %s", event_id)
            assert len(events) == 1
            assert events[0]["action"] == "ruleset.version_published"
            assert events[0]["aggregate_version"] == aggregate_version
