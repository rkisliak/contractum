"""The seeded platform rule set odcs-structural over HTTP and in the database (plan 05-13).

D-117, D-121, D-130 (VAL-01, RULE-05): migration 0005 creates a mandatory, enforced platform rule
set, so a validation run reports its structural rules with no setup by anyone, and no API route can
reach the set. The tests run against the stack's migrated database: no rule set is created by them.
"""

from __future__ import annotations

import base64
import importlib
import json
import uuid
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
import sqlalchemy as sa
import yaml
from dc_rules.model import content_sha256
from dc_validation.seed import (
    ODCS_CONSISTENCY_NAME,
    ODCS_CONSISTENCY_V1,
    ODCS_REFERENCES_NAME,
    ODCS_REFERENCES_V1,
    ODCS_STRUCTURAL_NAME,
    ODCS_STRUCTURAL_V1,
    PLATFORM_ODCS_CONSISTENCY_ID,
    PLATFORM_ODCS_CONSISTENCY_V1_ID,
    PLATFORM_ODCS_REFERENCES_ID,
    PLATFORM_ODCS_REFERENCES_V1_ID,
    PLATFORM_ODCS_STRUCTURAL_ID,
    PLATFORM_ODCS_STRUCTURAL_V1_ID,
    PLATFORM_SEED_EVENT_ID,
)
from psycopg.rows import dict_row

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

BASE = "/api/v1/validation"
PLATFORM_ID = str(PLATFORM_ODCS_STRUCTURAL_ID)
# Draft order of the rules in the seed, which is also key order.
RULE_KEYS = [
    "logical-type-option-bounds",
    "primary-key-positions-valid",
    "property-names-unique",
    "schema-names-unique",
]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def contract(properties: list[dict[str, Any]]) -> bytes:
    document = {
        "kind": "DataContract",
        "apiVersion": "v3.2.0",
        "id": "urn:datacontract:test:orders",
        "version": "1.0.0",
        "status": "draft",
        "schema": [{"name": "orders", "properties": properties}],
    }
    return yaml.safe_dump(document, sort_keys=False).encode()


def prop(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "logicalType": "string", **extra}


async def post_run(stack: Any, folder: str, token: str, document: bytes) -> Any:
    return await stack.client.post(
        f"/internal/v1/validation/folders/{folder}/runs",
        json={
            "mode": "publish",
            "media_type": "application/yaml",
            "document_b64": base64.b64encode(document).decode(),
            "attachments": [],
            "attachments_revision": 0,
            "contract_id": str(uuid.uuid4()),
            "draft_id": str(uuid.uuid4()),
        },
        headers=auth(token),
    )


async def team_editor(stack: Any) -> tuple[Any, str]:
    chain = await stack.chain()
    token, principal = stack.token("ed")
    stack.grant_sql(principal, "team_editor", chain.team)
    return chain, token


def structural(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in body["report"]["findings"] if f["rule_set"]["name"] == ODCS_STRUCTURAL_NAME]


async def test_the_migration_seeds_the_platform_rule_set_its_version_and_one_event(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        sets = stack.validation_sql("SELECT * FROM rule_set")
        assert (
            len(sets) == 4
        )  # odcs-structural, the two Phase 6 sets (0006) and odcs-publication (0009)
        rule_set = next(r for r in sets if r["name"] == ODCS_STRUCTURAL_NAME)
        assert rule_set["scope"] == "platform"
        assert rule_set["name"] == ODCS_STRUCTURAL_NAME
        assert rule_set["mandatory"] is True
        assert rule_set["publish_counter"] == 1
        assert rule_set["folder_id"] is None
        assert rule_set["folder_kind"] is None
        assert rule_set["ancestor_ids"] == []
        assert rule_set["created_by"] == "system:validation-seed"
        assert rule_set["id"] == PLATFORM_ODCS_STRUCTURAL_ID

        versions = stack.validation_sql(
            "SELECT *, enforce_at <= now() AS enforced FROM rule_set_version"
            " WHERE rule_set_id = %s",
            PLATFORM_ODCS_STRUCTURAL_ID,
        )
        assert len(versions) == 1
        version = versions[0]
        assert version["id"] == PLATFORM_ODCS_STRUCTURAL_V1_ID
        assert version["version_no"] == 1
        assert version["dsl_version"] == "cel-1"
        assert version["enforced"] is True
        assert version["rule_keys"] == RULE_KEYS  # draft order, which is key order
        assert version["content_sha256"] == content_sha256(ODCS_STRUCTURAL_V1)
        assert version["created_by"] == "system:validation-seed"

        events = stack.validation_sql(
            "SELECT * FROM outbox WHERE action = 'ruleset.version_published' AND event_id = %s",
            PLATFORM_SEED_EVENT_ID,
        )
        assert len(events) == 1
        event = events[0]
        assert event["event_id"] == PLATFORM_SEED_EVENT_ID
        assert event["aggregate_type"] == "rule_set"
        assert event["aggregate_id"] == PLATFORM_ID
        assert event["aggregate_version"] == 1
        assert event["actor"]["id"] == "system:validation-seed"
        assert event["actor"]["type"] == "system"
        assert event["scope_path"] == []
        assert event["after"]["version"]["rule_keys"] == RULE_KEYS
        assert event["after"]["version"]["content_sha256"] == version["content_sha256"]
        # Keys and hash only: no condition, target, message or fixture reaches the event.
        text = json.dumps(event["after"])
        for rule in ODCS_STRUCTURAL_V1.rules:
            assert rule.condition not in text
            assert rule.message not in text


async def test_a_publish_run_reports_the_platform_rule_a_duplicate_property_name_breaks(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)

        response = await post_run(
            stack, chain.team, editor, contract([prop("id"), prop("name"), prop("id")])
        )

        assert response.status_code == 200, response.text
        body = response.json()
        found = structural(body)
        assert len(found) == 1
        finding = found[0]
        assert finding["rule_key"] == "property-names-unique"
        assert finding["rule_set"]["scope"] == "platform"
        assert finding["rule_set"]["folder_id"] is None
        assert finding["rule_set"]["version"] == 1
        assert finding["source"] == "mandatory"
        assert finding["mode"] == "enforce"
        assert finding["outcome"] == "fail"
        assert finding["blocking"] is True
        assert finding["location"]["pointer"] == "/schema/0"
        assert body["report"]["summary"]["publishable"] is False
        pins = [p for p in body["pins"] if p["name"] == ODCS_STRUCTURAL_NAME]
        assert len(pins) == 1
        assert pins[0]["rule_set_id"] == PLATFORM_ID
        assert pins[0]["scope"] == "platform"
        assert pins[0]["folder_id"] is None
        assert pins[0]["version"] == 1
        assert pins[0]["source"] == "mandatory"
        assert pins[0]["mode"] == "enforce"


async def test_a_conformant_document_has_no_finding_from_the_platform_rule_set(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)
        document = contract(
            [
                prop("id", primaryKey=True, primaryKeyPosition=1),
                prop("region", primaryKey=True, primaryKeyPosition=2),
                prop("code", logicalTypeOptions={"minLength": 1, "maxLength": 4}),
            ]
        )

        response = await post_run(stack, chain.team, editor, document)

        assert response.status_code == 200, response.text
        body = response.json()
        assert structural(body) == []
        assert body["report"]["summary"]["publishable"] is True
        assert [p["version"] for p in body["pins"] if p["name"] == ODCS_STRUCTURAL_NAME] == [1]


async def test_the_platform_set_is_listed_first_for_any_folder_the_caller_can_read(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)

        response = await stack.get(f"{BASE}/folders/{chain.team}/available-rule-sets", token=editor)

        assert response.status_code == 200, response.text
        first = next(i for i in response.json()["items"] if i["id"] == PLATFORM_ID)
        assert first["name"] == ODCS_STRUCTURAL_NAME
        assert first["mandatory"] is True
        assert [(r["version_no"], r["mode"]) for r in first["resolved"]] == [(1, "enforce")]


async def test_no_author_route_reaches_the_platform_set(make_stack: Any) -> None:
    async with make_stack(platform_seed=True) as stack:
        admin = auth(stack.admin_token)
        rule_set = f"{BASE}/rule-sets/{PLATFORM_ID}"
        requests = [
            ("GET", rule_set, None),
            ("PUT", f"{rule_set}/draft", {"content": b"dsl_version: cel-1\nrules: []\n"}),
            ("PUT", f"{rule_set}/draft/rules/property-names-unique", {"json": {}}),
            ("DELETE", f"{rule_set}/draft/rules/property-names-unique", None),
            ("POST", f"{rule_set}/draft/check", None),
            ("POST", f"{rule_set}/publish", None),
            ("GET", f"{rule_set}/versions", None),
            ("GET", f"{rule_set}/versions/1", None),
            ("POST", f"{rule_set}/versions/1/deprecate", None),
            ("PUT", f"{rule_set}/mandatory", {"json": {"mandatory": False}}),
            ("POST", f"{rule_set}/versions/1/enforce-now", None),
            ("DELETE", rule_set, None),
        ]
        for method, url, extra in requests:
            response = await stack.client.request(
                method, url, headers={**admin, "If-Match": '"r0"'}, **(extra or {})
            )
            assert response.status_code == 404, (method, url, response.status_code, response.text)
            assert response.json()["type"] == "urn:dc:problem:not-found"

        assert (
            stack.validation_sql("SELECT mandatory, publish_counter FROM rule_set")
            == [{"mandatory": True, "publish_counter": 1}] * 4
        )
        assert len(stack.validation_sql("SELECT 1 FROM rule_set_version")) == 4


# -- idempotency, reversibility and drift (plan 05-13 task 2) ----------------------------------

DOWNGRADE_TARGET = "0004_validation_runs"


def seed_migration() -> Any:
    return importlib.import_module("dc_validation.migrations.versions.0005_seed_platform_rule_set")


def semantic_migration() -> Any:
    return importlib.import_module("dc_validation.migrations.versions.0006_seed_semantic_rule_sets")


def publication_migration() -> Any:
    return importlib.import_module(
        "dc_validation.migrations.versions.0009_seed_publication_rule_set"
    )


def counts(db_url: str) -> tuple[int, int, int]:
    url = db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(
            "SELECT (SELECT count(*) FROM rule_set), (SELECT count(*) FROM rule_set_version),"
            " (SELECT count(*) FROM outbox WHERE action = 'ruleset.version_published')"
        ).fetchone()
    assert row is not None
    return (int(row[0]), int(row[1]), int(row[2]))


async def test_the_stored_hash_is_the_hash_of_the_seed_constant(
    fresh_db_url: str, migrate_validation: Callable[..., None]
) -> None:
    migrate_validation()
    url = fresh_db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True) as conn:
        stored = conn.execute(
            "SELECT v.content_sha256, s.draft_content_sha256, v.rules = s.draft_rules"
            " FROM rule_set_version v JOIN rule_set s ON s.id = v.rule_set_id"
            " WHERE s.id = %s",
            (PLATFORM_ODCS_STRUCTURAL_ID,),
        ).fetchone()
    expected = content_sha256(ODCS_STRUCTURAL_V1)
    assert stored == (expected, expected, True)


async def test_running_the_seed_statements_again_changes_nothing(
    fresh_db_url: str, migrate_validation: Callable[..., None]
) -> None:
    migrate_validation()
    assert counts(fresh_db_url) == (4, 4, 4)
    engine = sa.create_engine(fresh_db_url)
    try:
        for _ in range(2):
            with engine.begin() as connection:
                for statement in [
                    *seed_migration().seed_statements(),
                    *semantic_migration().seed_statements(),
                    *publication_migration().seed_statements(),
                ]:
                    connection.execute(statement)
    finally:
        engine.dispose()
    assert counts(fresh_db_url) == (4, 4, 4)


async def test_downgrade_removes_the_seed_and_upgrade_recreates_it_without_drift(
    fresh_db_url: str, migrate_validation: Callable[..., None]
) -> None:
    migrate_validation()
    migrate_validation("downgrade", DOWNGRADE_TARGET)
    assert counts(fresh_db_url) == (0, 0, 0)
    migrate_validation("upgrade")
    assert counts(fresh_db_url) == (4, 4, 4)
    migrate_validation("check")  # raises CommandError on drift
    url = fresh_db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True) as conn:
        # The delete guard is back after the downgrade lifted it.
        with pytest.raises(psycopg.errors.RaiseException, match="cannot be deleted"):
            conn.execute("DELETE FROM rule_set_version")


# -- the semantic sets of migration 0006 (plan 06-09; D-150, D-151) ------------------------------

SEMANTIC = [
    (ODCS_CONSISTENCY_NAME, PLATFORM_ODCS_CONSISTENCY_ID, PLATFORM_ODCS_CONSISTENCY_V1_ID),
    (ODCS_REFERENCES_NAME, PLATFORM_ODCS_REFERENCES_ID, PLATFORM_ODCS_REFERENCES_V1_ID),
]


def semantic_rows(db_url: str) -> list[dict[str, Any]]:
    url = db_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        return conn.execute(
            "SELECT s.id AS set_id, s.name, s.scope, s.mandatory, s.folder_id,"
            " v.id AS version_id, v.version_no, v.dsl_version, v.content_sha256, v.rule_keys,"
            " extract(epoch FROM (v.enforce_at - now())) / 86400.0 AS days_to_enforce"
            " FROM rule_set s JOIN rule_set_version v ON v.rule_set_id = s.id"
            " WHERE s.name IN ('odcs-consistency', 'odcs-references') ORDER BY s.name"
        ).fetchall()


async def test_the_semantic_sets_are_seeded_mandatory_cel2_and_in_warn_grace(
    fresh_db_url: str, migrate_validation: Callable[..., None]
) -> None:
    migrate_validation()

    rows = semantic_rows(fresh_db_url)

    assert [r["name"] for r in rows] == ["odcs-consistency", "odcs-references"]
    for row, (_, set_id, version_id), content in zip(
        rows, SEMANTIC, (ODCS_CONSISTENCY_V1, ODCS_REFERENCES_V1), strict=True
    ):
        assert row["set_id"] == set_id
        assert row["version_id"] == version_id
        assert row["scope"] == "platform"
        assert row["folder_id"] is None
        assert row["mandatory"] is True
        assert row["version_no"] == 1
        assert row["dsl_version"] == "cel-2"
        assert row["content_sha256"] == content_sha256(content)
        assert row["rule_keys"] == [rule.key for rule in content.rules]
        # Default 14 days, taken at migration time: not enforced yet (D-150).
        assert 13.9 < float(row["days_to_enforce"]) <= 14.0


async def test_the_warn_grace_follows_the_mandatory_warn_days_setting(
    fresh_db_url: str,
    migrate_validation: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DC_MANDATORY_WARN_DAYS", "3")
    migrate_validation()

    assert [round(float(r["days_to_enforce"]), 1) for r in semantic_rows(fresh_db_url)] == [
        3.0,
        3.0,
    ]


async def test_zero_warn_days_enforces_the_semantic_sets_at_once(
    fresh_db_url: str,
    migrate_validation: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DC_MANDATORY_WARN_DAYS", "0")
    migrate_validation()

    assert all(float(r["days_to_enforce"]) <= 0.0 for r in semantic_rows(fresh_db_url))


async def test_running_the_semantic_seed_again_never_moves_the_enforcement_date(
    fresh_db_url: str,
    migrate_validation: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DC_MANDATORY_WARN_DAYS", "3")
    migrate_validation()
    before = [r["days_to_enforce"] for r in semantic_rows(fresh_db_url)]
    engine = sa.create_engine(fresh_db_url)
    try:
        with engine.begin() as connection:
            for statement in semantic_migration().seed_statements(warn_days=30):
                connection.execute(statement)
    finally:
        engine.dispose()

    assert counts(fresh_db_url) == (4, 4, 4)
    after = [r["days_to_enforce"] for r in semantic_rows(fresh_db_url)]
    assert all(abs(float(a) - float(b)) < 0.01 for a, b in zip(after, before, strict=True))


# -- a Global admin pauses and resumes a platform set (quick 261118-vpa) -----------------------

CONSISTENCY_ID = str(PLATFORM_ODCS_CONSISTENCY_ID)
DUPLICATE_PROPERTY = [prop("id"), prop("name"), prop("id")]


def paused_url(folder: str, rule_set_id: str) -> str:
    return f"{BASE}/folders/{folder}/platform-rule-sets/{rule_set_id}/paused"


async def put_paused(stack: Any, folder: str, rule_set_id: str, token: str, paused: bool) -> Any:
    return await stack.client.put(
        paused_url(folder, rule_set_id), json={"paused": paused}, headers=auth(token)
    )


def pause_events(stack: Any, action: str) -> list[dict[str, Any]]:
    return stack.validation_sql("SELECT * FROM outbox WHERE action = %s ORDER BY id", action)


async def test_pausing_a_platform_set_keeps_its_findings_but_stops_them_blocking(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)

        paused = await put_paused(stack, stack.root_id, PLATFORM_ID, stack.admin_token, True)

        assert paused.status_code == 200, paused.text
        out = paused.json()
        assert out["id"] == PLATFORM_ID
        assert out["paused_at"] is not None
        assert out["paused_by"] is not None
        response = await post_run(stack, chain.team, editor, contract(DUPLICATE_PROPERTY))
        assert response.status_code == 200, response.text
        body = response.json()
        (finding,) = structural(body)
        assert finding["rule_key"] == "property-names-unique"
        assert finding["outcome"] == "fail"
        assert finding["mode"] == "warn"
        assert finding["paused"] is True
        assert finding["blocking"] is False
        assert finding["enforce_at"] is None
        assert body["report"]["summary"]["publishable"] is True
        (pin,) = [p for p in body["pins"] if p["name"] == ODCS_STRUCTURAL_NAME]
        assert (pin["mode"], pin["paused"]) == ("warn", True)
        (counts_entry,) = [
            r for r in body["report"]["rule_sets"] if r["rule_set_id"] == PLATFORM_ID
        ]
        assert (counts_entry["mode"], counts_entry["paused"]) == ("warn", True)
        (event,) = pause_events(stack, "ruleset.paused")
        assert str(event["aggregate_id"]) == PLATFORM_ID
        assert event["before"]["paused_at"] is None
        assert event["after"]["paused_at"] is not None
        assert event["after"]["enforced_version_no"] == 1
        assert event["actor"]["id"] != ""


async def test_repeating_the_state_is_a_no_op_and_resume_blocks_again(make_stack: Any) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)
        await put_paused(stack, stack.root_id, PLATFORM_ID, stack.admin_token, True)

        again = await put_paused(stack, stack.root_id, PLATFORM_ID, stack.admin_token, True)
        assert again.status_code == 200
        assert len(pause_events(stack, "ruleset.paused")) == 1

        resumed = await put_paused(stack, stack.root_id, PLATFORM_ID, stack.admin_token, False)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["paused_at"] is None
        assert resumed.json()["paused_by"] is None
        (event,) = pause_events(stack, "ruleset.resumed")
        assert event["before"]["paused_at"] is not None
        assert event["after"]["paused_at"] is None
        assert event["after"]["paused_since"] is not None
        resumed_again = await put_paused(
            stack, stack.root_id, PLATFORM_ID, stack.admin_token, False
        )
        assert resumed_again.status_code == 200
        assert len(pause_events(stack, "ruleset.resumed")) == 1

        response = await post_run(stack, chain.team, editor, contract(DUPLICATE_PROPERTY))
        body = response.json()
        (finding,) = structural(body)
        assert (finding["mode"], finding["paused"], finding["blocking"]) == ("enforce", False, True)
        assert body["report"]["summary"]["publishable"] is False


async def test_pause_and_resume_of_a_warning_set_keep_its_date_and_show_in_available_sets(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)
        path = f"{BASE}/folders/{chain.team}/available-rule-sets"

        async def consistency() -> dict[str, Any]:
            listed = await stack.get(path, token=editor)
            return next(i for i in listed.json()["items"] if i["id"] == CONSISTENCY_ID)

        seeded = (await consistency())["resolved"][0]
        assert seeded["mode"] == "warn"
        assert seeded["paused"] is False

        await put_paused(stack, stack.root_id, CONSISTENCY_ID, stack.admin_token, True)
        paused = await consistency()
        assert paused["paused"] is True
        assert paused["paused_at"] is not None
        assert paused["resolved"][0]["paused"] is True
        assert paused["resolved"][0]["mode"] == "warn"

        await put_paused(stack, stack.root_id, CONSISTENCY_ID, stack.admin_token, False)
        resumed = await consistency()
        assert resumed["paused"] is False
        assert resumed["paused_at"] is None
        assert resumed["resolved"][0]["paused"] is False
        assert resumed["resolved"][0]["mode"] == "warn"
        assert resumed["resolved"][0]["enforce_at"] == seeded["enforce_at"]


async def test_pausing_is_refused_uniformly_for_anyone_else(make_stack: Any) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        pam, pam_id = stack.token("pam")
        stack.grant_sql(pam_id, "policy_manager", chain.org)
        reader, reader_id = stack.token("reader")
        stack.grant_sql(reader_id, "global_reader", stack.root_id)
        root_pam, root_pam_id = stack.token("rootpam")
        stack.grant_sql(root_pam_id, "policy_manager", stack.root_id)
        created = await stack.create_rule_set(chain.org, "Folder scoped", token=root_pam)
        assert created.status_code == 201, created.text
        folder_set: str = created.json()["id"]

        anonymous = await stack.client.put(
            paused_url(stack.root_id, PLATFORM_ID), json={"paused": True}
        )
        below = await put_paused(stack, stack.root_id, PLATFORM_ID, pam, True)
        read_only = await put_paused(stack, stack.root_id, PLATFORM_ID, reader, True)

        assert anonymous.status_code == 401
        assert below.status_code == 404, below.text
        assert read_only.status_code in (403, 404), read_only.text
        for folder, rule_set in [
            (chain.org, PLATFORM_ID),  # not the ROOT folder
            (stack.root_id, folder_set),  # a folder-owned set
            (stack.root_id, str(uuid.uuid4())),  # unknown
        ]:
            response = await put_paused(stack, folder, rule_set, root_pam, True)
            assert response.status_code == 404, (folder, rule_set, response.text)
            assert response.json()["type"] == "urn:dc:problem:not-found"
        invalid = await stack.client.put(
            paused_url(stack.root_id, PLATFORM_ID),
            json={"paused": True, "extra": 1},
            headers=auth(root_pam),
        )
        assert invalid.status_code == 422
        assert pause_events(stack, "ruleset.paused") == []
        assert stack.validation_sql("SELECT 1 FROM rule_set WHERE paused_at IS NOT NULL") == []


async def test_the_database_refuses_to_pause_a_folder_owned_set(make_stack: Any) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        root_pam, root_pam_id = stack.token("rootpam")
        stack.grant_sql(root_pam_id, "policy_manager", stack.root_id)
        created = await stack.create_rule_set(chain.org, "Folder scoped", token=root_pam)
        assert created.status_code == 201, created.text

        with pytest.raises(psycopg.errors.CheckViolation):
            stack.validation_sql(
                "UPDATE rule_set SET paused_at = now() WHERE id = %s", created.json()["id"]
            )
