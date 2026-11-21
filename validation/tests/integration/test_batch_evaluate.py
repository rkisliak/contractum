"""Rule impact: the internal dry-run and drift routes through the real worker (Plan 06-11).

RULE-10 (D-159): a rule-set version is evaluated as if it were enforced against a page of
documents. RULE-11 (D-160): each document is evaluated against the current mandatory resolution of
its own folder. Contracts (Plan 06-12) sends the pages with the caller's own token; here the tests
post to the internal routes directly with fake-issuer bearers, exactly as contracts will.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any

import pytest
from dc_validation.seed import PLATFORM_ODCS_STRUCTURAL_ID

from .test_runs import as_yaml, auth, contract, publish_next, rule_body, setup

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

BASE = "/api/v1/validation"
INTERNAL = "/internal/v1/validation"
RULE_KEY = "unique-property-names"
MESSAGE = "Property names must be unique."


def dry_run_url(rule_set_id: str) -> str:
    return f"{INTERNAL}/rule-sets/{rule_set_id}/dry-run"


def item(folder_id: str, document: bytes, contract_id: str | None = None) -> dict[str, Any]:
    return {
        "contract_id": contract_id or str(uuid.uuid4()),
        "folder_id": folder_id,
        "media_type": "application/yaml",
        "document_b64": base64.b64encode(document).decode(),
    }


def dry_run_body(
    scope_folder_id: str, items: list[dict[str, Any]], version: int | str = "draft"
) -> dict[str, Any]:
    return {"version": version, "scope_folder_id": scope_folder_id, "items": items}


async def draft_set(stack: Any, folder: str, token: str, *, name: str = "Draft naming") -> str:
    """A rule set whose draft holds the unique-property-names rule (never published)."""
    created = await stack.create_rule_set(folder, name, token=token)
    assert created.status_code == 201, created.text
    rule_set_id: str = created.json()["id"]
    put = await stack.put_rule(
        rule_set_id, RULE_KEY, rule_body(MESSAGE), created.headers["ETag"], token=token
    )
    assert put.status_code == 200, put.text
    return rule_set_id


def counts(stack: Any) -> tuple[int, int]:
    runs = stack.validation_sql("SELECT count(*) AS n FROM validation_run")[0]["n"]
    outbox = stack.validation_sql("SELECT count(*) AS n FROM outbox")[0]["n"]
    return runs, outbox


# -- tracer ---------------------------------------------------------------------------------------


async def test_a_policy_managers_draft_rule_is_dry_run_and_reports_would_block(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, _ted, _ = await setup(stack)
        rule_set_id = await draft_set(stack, chain.org, pam)
        contract_id = str(uuid.uuid4())
        before = counts(stack)

        response = await stack.client.post(
            dry_run_url(rule_set_id),
            json=dry_run_body(
                chain.org, [item(chain.team, as_yaml(contract("id", "id")), contract_id)]
            ),
            headers=auth(pam),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["rule_set_id"] == rule_set_id
        assert body["version"] == "draft"
        assert body["totals"] == {"would_fail": 1, "would_error": 0, "pass": 0}
        [result] = body["items"]
        assert result["contract_id"] == contract_id
        assert result["status"] == "evaluated"
        assert result["would_block"] is True
        assert result["counts"] == {"fail": 1, "error": 0, "warn": 0}
        [failing] = result["failing_rules"]
        assert failing["rule_key"] == RULE_KEY
        assert failing["rule_set_id"] == rule_set_id
        assert failing["version_no"] is None
        assert failing["mode"] == "enforce"
        assert failing["severity"] == "error"
        assert failing["outcome"] == "fail"
        assert failing["message"] == MESSAGE
        assert failing["pointers"] == ["/schema/0"]
        assert failing["pointers_total"] == 1
        # Nothing is stored and nothing is audited (D-158, D-161).
        assert counts(stack) == before


async def test_a_team_editor_and_a_platform_rule_set_get_the_uniform_404(make_stack: Any) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, pam, ted, _ = await setup(stack)
        rule_set_id = await draft_set(stack, chain.org, pam)
        payload = dry_run_body(chain.org, [item(chain.team, as_yaml(contract("id", "id")))])

        editor = await stack.client.post(dry_run_url(rule_set_id), json=payload, headers=auth(ted))
        assert editor.status_code == 404, editor.text

        platform = await stack.client.post(
            dry_run_url(str(PLATFORM_ODCS_STRUCTURAL_ID)),
            json=dry_run_body(stack.root_id, payload["items"], version=1),
            headers=auth(stack.admin_token),
        )
        assert platform.status_code == 404, platform.text

        unknown = await stack.client.post(
            dry_run_url(str(uuid.uuid4())), json=payload, headers=auth(pam)
        )
        assert unknown.status_code == 404, unknown.text

        anonymous = await stack.client.post(dry_run_url(rule_set_id), json=payload)
        assert anonymous.status_code == 401, anonymous.text


async def test_a_published_version_is_dry_run_and_a_passing_document_passes(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, _ted, _ = await setup(stack)
        created = await stack.create_rule_set(chain.org, "Published naming", token=pam)
        rule_set_id = created.json()["id"]
        await publish_next(stack, rule_set_id, pam, rule_body(MESSAGE), RULE_KEY)

        response = await stack.client.post(
            dry_run_url(rule_set_id),
            json=dry_run_body(
                chain.org,
                [
                    item(chain.team, as_yaml(contract("id", "name"))),
                    item(chain.project, as_yaml(contract("id", "id"))),
                ],
                version=1,
            ),
            headers=auth(stack.admin_token),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["version"] == 1
        assert body["totals"] == {"would_fail": 1, "would_error": 0, "pass": 1}
        passing, failing = body["items"]
        assert passing["would_block"] is False and passing["failing_rules"] == []
        assert failing["failing_rules"][0]["version_no"] == 1

        missing = await stack.client.post(
            dry_run_url(rule_set_id),
            json=dry_run_body(chain.org, [], version=2),
            headers=auth(stack.admin_token),
        )
        assert missing.status_code == 404, missing.text


async def test_results_carry_no_document_content(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam, _ted, _ = await setup(stack)
        rule_set_id = await draft_set(stack, chain.org, pam)
        secret = "SECRETCOLUMN"
        document = contract(secret, secret)
        document["schema"][0]["properties"][0]["description"] = "TOPSECRETDESCRIPTION"

        response = await stack.client.post(
            dry_run_url(rule_set_id),
            json=dry_run_body(chain.org, [item(chain.team, as_yaml(document))]),
            headers=auth(pam),
        )

        assert response.status_code == 200, response.text
        text = json.dumps(response.json())
        assert secret not in text
        assert "TOPSECRET" not in text
        assert "orders" not in text


async def test_dry_run_items_outside_the_scope_make_the_whole_call_404(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam, _ted, _ = await setup(stack)
        rule_set_id = await draft_set(stack, chain.org, pam)
        other = await stack.chain()
        stack.grant_sql("fake:pam", "policy_manager", other.org)
        failing = as_yaml(contract("id", "id"))

        # an item in another Organization, even one the caller manages, is outside the rule set
        outside = await stack.client.post(
            dry_run_url(rule_set_id),
            json=dry_run_body(chain.org, [item(chain.team, failing), item(other.team, failing)]),
            headers=auth(pam),
        )
        assert outside.status_code == 404, outside.text

        # the scope folder must be the rule set's folder or below it
        wide = await stack.client.post(
            dry_run_url(rule_set_id),
            json=dry_run_body(other.org, [item(other.team, failing)]),
            headers=auth(pam),
        )
        assert wide.status_code == 404, wide.text

        # narrowed to the team: a project item is outside the narrowed scope
        narrowed = await stack.client.post(
            dry_run_url(rule_set_id),
            json=dry_run_body(chain.team, [item(chain.project, failing)]),
            headers=auth(pam),
        )
        assert narrowed.status_code == 404, narrowed.text

        inside = await stack.client.post(
            dry_run_url(rule_set_id),
            json=dry_run_body(chain.team, [item(chain.team, failing)]),
            headers=auth(pam),
        )
        assert inside.status_code == 200, inside.text


# -- drift ----------------------------------------------------------------------------------------


def drift_url(folder_id: str) -> str:
    return f"{INTERNAL}/folders/{folder_id}/drift"


async def mandatory_set(stack: Any, folder: str, token: str, *, enforce: bool, name: str) -> str:
    """A published, mandatory rule set at ``folder``; enforced now or left in its warn grace."""
    created = await stack.create_rule_set(folder, name, token=token)
    assert created.status_code == 201, created.text
    rule_set_id: str = created.json()["id"]
    await publish_next(stack, rule_set_id, token, rule_body(MESSAGE), RULE_KEY)
    mandated = await stack.client.put(
        f"{BASE}/rule-sets/{rule_set_id}/mandatory", json={"mandatory": True}, headers=auth(token)
    )
    assert mandated.status_code == 200, mandated.text
    if enforce:
        enforced = await stack.client.post(
            f"{BASE}/rule-sets/{rule_set_id}/versions/1/enforce-now", headers=auth(token)
        )
        assert enforced.status_code == 200, enforced.text
    return rule_set_id


async def test_drift_lists_a_failing_document_as_drifted_under_the_enforced_version(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, _ted, _ = await setup(stack)
        rule_set_id = await mandatory_set(stack, chain.org, pam, enforce=True, name="Enforced")
        before = counts(stack)
        drifting, compliant = str(uuid.uuid4()), str(uuid.uuid4())

        response = await stack.client.post(
            drift_url(chain.org),
            json={
                "items": [
                    item(chain.team, as_yaml(contract("id", "id")), drifting),
                    item(chain.team, as_yaml(contract("id", "name")), compliant),
                ]
            },
            headers=auth(pam),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["folder_id"] == chain.org
        assert body["totals"] == {"drifted": 1, "at_risk": 0, "compliant": 1, "unparseable": 0}
        first, second = body["items"]
        assert first["contract_id"] == drifting
        assert first["folder_id"] == chain.team
        assert first["status"] == "drifted"
        assert first["counts"] == {"fail": 1, "error": 0, "warn": 0}
        [failing] = first["failing_rules"]
        assert failing["rule_set_id"] == rule_set_id
        assert (failing["version_no"], failing["mode"], failing["blocking"]) == (1, "enforce", True)
        assert failing["pointers"] == ["/schema/0"]
        current = {
            "rule_set_id": rule_set_id,
            "name": "Enforced",
            "version_no": 1,
            "mode": "enforce",
        }
        assert first["current_versions"] == [current]
        assert second["contract_id"] == compliant
        assert second["status"] == "compliant"
        assert second["failing_rules"] == []
        assert second["current_versions"] == [current]
        # purely informational: no stored run, no outbox row (D-160)
        assert counts(stack) == before


async def test_drift_lists_a_document_failing_only_a_warn_mode_version_as_at_risk(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, _ted, _ = await setup(stack)
        rule_set_id = await mandatory_set(stack, chain.org, pam, enforce=False, name="Grace")

        response = await stack.client.post(
            drift_url(chain.org),
            json={"items": [item(chain.team, as_yaml(contract("id", "id")))]},
            headers=auth(pam),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["totals"] == {"drifted": 0, "at_risk": 1, "compliant": 0, "unparseable": 0}
        [result] = body["items"]
        assert result["status"] == "at_risk"
        assert result["failing_rules"][0]["mode"] == "warn"
        assert result["failing_rules"][0]["blocking"] is False
        assert result["current_versions"] == [
            {"rule_set_id": rule_set_id, "name": "Grace", "version_no": 1, "mode": "warn"}
        ]


async def test_drift_resolves_each_item_for_its_own_folder_and_refuses_items_outside(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, _ted, _ = await setup(stack)
        sibling = await stack.folder(chain.project, "sibling")
        await mandatory_set(stack, chain.team, pam, enforce=True, name="Team only")
        failing = as_yaml(contract("id", "id"))

        response = await stack.client.post(
            drift_url(chain.org),
            json={"items": [item(chain.team, failing), item(sibling, failing)]},
            headers=auth(pam),
        )

        assert response.status_code == 200, response.text
        team_item, sibling_item = response.json()["items"]
        assert team_item["status"] == "drifted"
        assert sibling_item["status"] == "compliant"
        assert sibling_item["current_versions"] == []

        other = await stack.chain()
        stack.grant_sql("fake:pam", "policy_manager", other.org)
        outside = await stack.client.post(
            drift_url(chain.org),
            json={"items": [item(chain.team, failing), item(other.team, failing)]},
            headers=auth(pam),
        )
        assert outside.status_code == 404, outside.text


async def test_an_org_admin_reads_drift_and_a_team_editor_gets_404(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam, ted, _ = await setup(stack)
        await mandatory_set(stack, chain.org, pam, enforce=True, name="Enforced")
        oda, oda_id = stack.token("oda")
        stack.grant_sql(oda_id, "org_admin", chain.org)
        payload = {"items": [item(chain.team, as_yaml(contract("id", "id")))]}

        admin = await stack.client.post(drift_url(chain.org), json=payload, headers=auth(oda))
        assert admin.status_code == 200, admin.text
        assert admin.json()["items"][0]["status"] == "drifted"

        editor = await stack.client.post(drift_url(chain.org), json=payload, headers=auth(ted))
        assert editor.status_code == 404, editor.text


# -- capacity (D-161) -----------------------------------------------------------------------------

# 4 GiB of string: bounded by the worker's memory limit (Linux) or the per-rule deadline.
DOUBLING = "['aaaaaaaaaaaaaaaa']" + ".map(s, s+s)" * 28 + "[0].size() > 0"


async def test_saturated_batch_slots_answer_503_while_a_publish_run_still_completes(
    make_stack: Any,
) -> None:
    # max_concurrent_runs 2 gives one batch slot; holding it saturates the batch pool only.
    async with make_stack(max_concurrent_runs=2, queue_wait_s=0.3) as stack:
        chain, pam, ted, _ = await setup(stack)
        rule_set_id = await draft_set(stack, chain.org, pam)
        sandbox = stack.app.state.sandbox
        publish = {
            "mode": "publish",
            "contract_id": str(uuid.uuid4()),
            "draft_id": str(uuid.uuid4()),
            "media_type": "application/yaml",
            "document_b64": base64.b64encode(as_yaml(contract("id", "name"))).decode(),
            "attachments": [],
            "attachments_revision": 0,
        }

        async with sandbox.batch_slot():
            batch, run = await asyncio.gather(
                stack.client.post(
                    dry_run_url(rule_set_id),
                    json=dry_run_body(chain.org, [item(chain.team, as_yaml(contract("id")))]),
                    headers=auth(pam),
                ),
                stack.client.post(
                    f"{INTERNAL}/folders/{chain.team}/runs", json=publish, headers=auth(ted)
                ),
            )

        assert batch.status_code == 503, batch.text
        assert batch.headers["Retry-After"] == "1"
        assert batch.json()["type"] == "urn:dc:problem:validation-unavailable"
        assert run.status_code == 200, run.text
        assert run.json()["run_id"] is not None

        # once the slot is free again the page is evaluated
        again = await stack.client.post(
            dry_run_url(rule_set_id),
            json=dry_run_body(chain.org, [item(chain.team, as_yaml(contract("id")))]),
            headers=auth(pam),
        )
        assert again.status_code == 200, again.text


def assert_cap_refused(response: Any, pointer: str, *secrets: str) -> None:
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["type"] == "urn:dc:problem:validation-error"
    assert [e["pointer"] for e in body["errors"]] == [pointer]
    for secret in secrets:
        assert secret not in response.text


async def test_pages_over_the_item_byte_or_folder_cap_are_refused_before_evaluation(
    make_stack: Any,
) -> None:
    async with make_stack(max_batch_bytes=6000) as stack:
        chain, pam, _ted, _ = await setup(stack)
        rule_set_id = await draft_set(stack, chain.org, pam)
        small = as_yaml(contract("id"))

        too_many = await stack.client.post(
            drift_url(chain.org),
            json={"items": [item(chain.team, small) for _ in range(26)]},
            headers=auth(pam),
        )
        assert_cap_refused(too_many, "/items")

        secret = "SECRETNAME" + "x" * 3100
        big = as_yaml(contract(secret))
        too_big = await stack.client.post(
            dry_run_url(rule_set_id),
            json=dry_run_body(chain.org, [item(chain.team, big), item(chain.team, big)]),
            headers=auth(pam),
        )
        assert_cap_refused(too_big, "/items", "SECRETNAME")

        folders = [str(uuid.uuid4()) for _ in range(21)]
        too_spread = await stack.client.post(
            drift_url(chain.org),
            json={"items": [item(f, small) for f in folders]},
            headers=auth(pam),
        )
        assert_cap_refused(too_spread, "/items", *folders)

        at_cap = await stack.client.post(
            drift_url(chain.org),
            json={"items": [item(chain.team, small) for _ in range(25)]},
            headers=auth(pam),
        )
        assert at_cap.status_code == 200, at_cap.text


async def test_a_hostile_draft_rule_is_an_error_outcome_in_a_dry_run_not_a_hang(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, _ted, _ = await setup(stack)
        created = await stack.create_rule_set(chain.org, "Hostile draft", token=pam)
        rule_set_id = created.json()["id"]
        hostile = json.dumps(
            {
                "target": "$",
                "condition": DOUBLING,
                "severity": "error",
                "message": "Strings must stay small.",
                "fixtures": {"pass": [], "fail": []},
            }
        )
        put = await stack.put_rule(
            rule_set_id, "doubling", hostile, created.headers["ETag"], token=pam
        )
        assert put.status_code == 200, put.text

        response = await stack.client.post(
            dry_run_url(rule_set_id),
            json=dry_run_body(chain.org, [item(chain.team, as_yaml(contract("id")))]),
            headers=auth(pam),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["totals"] == {"would_fail": 0, "would_error": 1, "pass": 0}
        [result] = body["items"]
        assert result["would_block"] is True
        assert result["counts"] == {"fail": 0, "error": 1, "warn": 0}
        [failing] = result["failing_rules"]
        assert failing["outcome"] == "error"
        assert failing["error_code"] in {"timeout", "memory_exceeded", "budget_exceeded"}
        assert "Strings must stay small." != failing["message"]  # a fixed error text instead


async def test_a_page_past_its_budget_stops_and_answers_503(make_stack: Any) -> None:
    """WR-03: a page never keeps evaluating after its budget (contracts gives up at 30 s)."""
    async with make_stack(batch_page_budget_s=0.000001) as stack:
        chain, pam, _, _ = await setup(stack)
        rule_set_id = await draft_set(stack, chain.org, pam)
        documents = [item(chain.team, as_yaml(contract("id", f"c{i}"))) for i in range(3)]

        response = await stack.client.post(
            dry_run_url(rule_set_id), json=dry_run_body(chain.org, documents), headers=auth(pam)
        )

        assert response.status_code == 503, response.text
        assert response.json()["type"] == "urn:dc:problem:validation-unavailable"
        assert response.headers["Retry-After"] == "1"


async def test_the_default_batch_pool_leaves_a_worker_for_publish_runs(make_stack: Any) -> None:
    """WR-03: with two workers the batch pool is one slot, so a page never holds both."""
    async with make_stack(eval_workers=2, max_concurrent_runs=4) as stack:
        settings = stack.app.state.settings
        assert settings.batch_slot_count() == 1 < settings.eval_workers


# -- fail-closed edges ----------------------------------------------------------------------------

UNPARSEABLE = b"schema: [unclosed"


async def test_a_document_that_does_not_parse_is_unparseable_and_counts_as_blocking(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, _ted, _ = await setup(stack)
        rule_set_id = await mandatory_set(stack, chain.org, pam, enforce=True, name="Enforced")
        broken, good = str(uuid.uuid4()), str(uuid.uuid4())
        page = [
            item(chain.team, UNPARSEABLE, broken),
            item(chain.team, as_yaml(contract("id", "name")), good),
        ]

        dry = await stack.client.post(
            dry_run_url(rule_set_id), json=dry_run_body(chain.org, page, 1), headers=auth(pam)
        )
        assert dry.status_code == 200, dry.text
        assert dry.json()["totals"] == {"would_fail": 0, "would_error": 1, "pass": 1}
        unparseable, passed = dry.json()["items"]
        assert unparseable == {
            "contract_id": broken,
            "status": "unparseable",
            "would_block": True,
            "counts": {"fail": 0, "error": 0, "warn": 0},
            "failing_rules": [],
        }
        assert (passed["contract_id"], passed["would_block"]) == (good, False)

        drift = await stack.client.post(
            drift_url(chain.org), json={"items": page}, headers=auth(pam)
        )
        assert drift.status_code == 200, drift.text
        totals = drift.json()["totals"]
        assert totals == {"drifted": 0, "at_risk": 0, "compliant": 1, "unparseable": 1}
        first = drift.json()["items"][0]
        assert (first["contract_id"], first["status"], first["failing_rules"]) == (
            broken,
            "unparseable",
            [],
        )
        assert "unclosed" not in dry.text + drift.text  # nothing of the document is echoed


async def test_a_page_body_of_the_wrong_shape_is_a_422_before_anything_runs(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, _ted, _ = await setup(stack)
        rule_set_id = await draft_set(stack, chain.org, pam)

        no_version = await stack.client.post(
            dry_run_url(rule_set_id),
            json={"scope_folder_id": chain.org, "items": []},
            headers=auth(pam),
        )
        bad_items = await stack.client.post(
            drift_url(chain.org), json={"items": "not a list"}, headers=auth(pam)
        )
        for refused in (no_version, bad_items):
            assert refused.status_code == 422, refused.text
            assert refused.json()["type"] == "urn:dc:problem:validation-error"
        assert [e["pointer"] for e in no_version.json()["errors"]] == ["/version"]
        assert [e["pointer"] for e in bad_items.json()["errors"]] == ["/items"]
