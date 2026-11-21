"""The seeded semantic platform rule sets in real publish runs (plan 06-09; D-150, D-151).

``odcs-consistency`` and ``odcs-references`` (migration 0006) are mandatory but start in warn
grace, so a finding is reported and never blocks until a Global Policy Manager enforces early. The
tests run against the stack's migrated database with the seed kept (``platform_seed=True``).
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml
from dc_validation.seed import PLATFORM_ODCS_CONSISTENCY_ID, PLATFORM_ODCS_REFERENCES_ID

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

CONSISTENCY = "odcs-consistency"
REFERENCES = "odcs-references"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def contract(properties: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "kind": "DataContract",
        "apiVersion": "v3.2.0",
        "id": "urn:datacontract:test:orders",
        "version": "1.0.0",
        "status": "draft",
        "schema": [{"name": "orders", "properties": properties}],
    }
    document.update(extra)
    return document


def prop(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "logicalType": "string", **extra}


async def post_run(
    stack: Any,
    folder: str,
    token: str,
    document: dict[str, Any],
    ref_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "mode": "publish",
        "media_type": "application/yaml",
        "document_b64": base64.b64encode(
            yaml.safe_dump(document, sort_keys=False).encode()
        ).decode(),
        "attachments": [],
        "attachments_revision": 0,
        "contract_id": str(uuid.uuid4()),
        "draft_id": str(uuid.uuid4()),
    }
    if ref_facts is not None:
        body["ref_facts"] = ref_facts
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


def of_set(body: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [f for f in body["report"]["findings"] if f["rule_set"]["name"] == name]


async def test_a_primary_key_that_is_not_unique_is_reported_in_warn_mode_then_blocks_once_enforced(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)
        document = contract([prop("id", primaryKey=True, required=True), prop("name")])

        body = await post_run(stack, chain.team, editor, document)

        found = of_set(body, CONSISTENCY)
        assert [f["rule_key"] for f in found] == ["primary-key-required-unique"]
        finding = found[0]
        assert finding["outcome"] == "fail"
        assert finding["severity"] == "error"
        assert finding["source"] == "mandatory"
        assert finding["mode"] == "warn"
        assert finding["blocking"] is False
        assert finding["rule_set"]["scope"] == "platform"
        assert finding["location"]["pointer"] == "/schema/0"
        assert body["report"]["summary"]["publishable"] is True
        pin = next(p for p in body["pins"] if p["name"] == CONSISTENCY)
        assert (pin["version"], pin["mode"]) == (1, "warn")

        # What enforce-now does to the version (the route itself is covered by the Global Policy
        # Manager tests below).
        stack.validation_sql(
            "UPDATE rule_set_version SET enforce_at = now() - interval '1 second'"
            " WHERE rule_set_id IN (SELECT id FROM rule_set WHERE name = %s)",
            CONSISTENCY,
        )
        enforced = await post_run(stack, chain.team, editor, document)

        blocked = of_set(enforced, CONSISTENCY)
        assert [(f["mode"], f["blocking"]) for f in blocked] == [("enforce", True)]
        assert enforced["report"]["summary"]["publishable"] is False
        # The references set is still in grace and has nothing to say about this document.
        assert of_set(enforced, REFERENCES) == []


async def test_a_conformant_document_has_no_finding_from_either_semantic_set(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)
        document = contract([prop("id", primaryKey=True, required=True, unique=True)])

        body = await post_run(stack, chain.team, editor, document)

        assert of_set(body, CONSISTENCY) == []
        assert of_set(body, REFERENCES) == []
        assert {p["name"] for p in body["pins"]} >= {CONSISTENCY, REFERENCES}


LINK = "/api/v1/contracts/contracts/11111111-1111-4111-8111-111111111111"


async def test_a_ghost_team_member_and_a_missing_linked_contract_are_reported_in_warn_mode(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)
        document = contract(
            [prop("id")],
            team={"name": "orders", "members": [{"username": "ghost"}]},
            authoritativeDefinitions=[{"type": "businessDefinition", "url": LINK}],
        )

        body = await post_run(
            stack, chain.team, editor, document, {"contracts": {LINK: {"state": "not_found"}}}
        )

        found = {f["rule_key"]: f for f in of_set(body, REFERENCES)}
        assert found["team-member-exists"]["severity"] == "warning"
        assert found["team-member-exists"]["blocking"] is False
        assert found["linked-contract-published"]["severity"] == "error"
        assert found["linked-contract-published"]["mode"] == "warn"
        assert found["linked-contract-published"]["blocking"] is False
        assert found["linked-contract-published"]["location"]["pointer"] == (
            "/authoritativeDefinitions/0"
        )
        assert body["report"]["summary"]["publishable"] is True


async def test_a_link_nobody_could_check_never_produces_a_failing_finding(make_stack: Any) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)
        document = contract(
            [prop("id")], authoritativeDefinitions=[{"type": "businessDefinition", "url": LINK}]
        )

        unverifiable = await post_run(stack, chain.team, editor, document)  # no facts sent
        published = await post_run(
            stack,
            chain.team,
            editor,
            document,
            {"contracts": {LINK: {"state": "published", "pinned_published": True}}},
        )

        assert of_set(unverifiable, REFERENCES) == []
        assert of_set(published, REFERENCES) == []


async def test_a_pii_property_needs_a_classification_and_a_retention_sla(make_stack: Any) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)
        bare = contract([prop("email", tags=["PII"])])
        covered = contract(
            [prop("email", tags=["PII"], classification="restricted")],
            slaProperties=[
                {"property": "retention", "value": 5, "unit": "y", "element": "orders.email"}
            ],
        )

        missing = await post_run(stack, chain.team, editor, bare)
        satisfied = await post_run(stack, chain.team, editor, covered)

        found = {f["rule_key"]: f for f in of_set(missing, CONSISTENCY)}
        assert set(found) == {"pii-needs-classification", "pii-needs-retention"}
        assert found["pii-needs-classification"]["severity"] == "warning"
        assert found["pii-needs-classification"]["location"]["pointer"] == "/schema/0/properties/0"
        assert found["pii-needs-retention"]["blocking"] is False
        assert of_set(satisfied, CONSISTENCY) == []


async def test_an_sla_element_that_does_not_exist_is_an_error_finding_in_warn_mode(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)
        document = contract(
            [prop("id")],
            slaProperties=[
                {"property": "latency", "value": 1, "unit": "d", "element": "orders.zz"}
            ],
        )

        body = await post_run(stack, chain.team, editor, document)

        found = of_set(body, CONSISTENCY)
        assert [(f["rule_key"], f["severity"], f["blocking"]) for f in found] == [
            ("sla-element-resolves", "error", False)
        ]
        assert found[0]["location"]["pointer"] == "/slaProperties/0"


# -- a Global Policy Manager enforces a platform set early (D-150) -----------------------------

CONSISTENCY_ID = str(PLATFORM_ODCS_CONSISTENCY_ID)
REFERENCES_ID = str(PLATFORM_ODCS_REFERENCES_ID)


def platform_url(folder: str, rule_set_id: str, version_no: int = 1) -> str:
    return (
        f"/api/v1/validation/folders/{folder}/platform-rule-sets/{rule_set_id}"
        f"/versions/{version_no}/enforce-now"
    )


def enforce_at_url(folder: str, rule_set_id: str, version_no: int = 1) -> str:
    return (
        f"/api/v1/validation/folders/{folder}/platform-rule-sets/{rule_set_id}"
        f"/versions/{version_no}/enforce-at"
    )


async def put_enforce_at(
    stack: Any, folder: str, rule_set_id: str, token: str, when: str, version_no: int = 1
) -> Any:
    return await stack.client.put(
        enforce_at_url(folder, rule_set_id, version_no),
        json={"enforce_at": when},
        headers=auth(token),
    )


async def test_a_global_policy_manager_enforces_a_platform_set_early_and_it_is_audited(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)
        pam, pam_id = stack.token("pam")
        stack.grant_sql(pam_id, "policy_manager", stack.root_id)
        document = contract([prop("id", primaryKey=True, required=True)])
        before = await post_run(stack, chain.team, editor, document)
        assert before["report"]["summary"]["publishable"] is True

        response = await stack.client.post(
            platform_url(stack.root_id, CONSISTENCY_ID), headers=auth(pam)
        )

        assert response.status_code == 200, response.text
        out = response.json()
        assert out["version_no"] == 1
        assert out["enforce_at"] is not None
        events = stack.validation_sql(
            "SELECT * FROM outbox WHERE action = 'ruleset.enforced_early'"
        )
        assert len(events) == 1
        assert str(events[0]["aggregate_id"]) == CONSISTENCY_ID
        assert events[0]["actor"]["id"] == pam_id
        assert events[0]["after"]["previous_enforce_at"] is not None
        after = await post_run(stack, chain.team, editor, document)
        assert [(f["rule_key"], f["blocking"]) for f in of_set(after, CONSISTENCY)] == [
            ("primary-key-required-unique", True)
        ]
        assert after["report"]["summary"]["publishable"] is False
        # The other platform set is untouched and stays in grace.
        assert of_set(after, REFERENCES) == []
        pin = next(p for p in after["pins"] if p["name"] == REFERENCES)
        assert pin["mode"] == "warn"

        repeat = await stack.client.post(
            platform_url(stack.root_id, CONSISTENCY_ID), headers=auth(pam)
        )
        assert repeat.status_code == 200
        assert (
            len(
                stack.validation_sql("SELECT 1 FROM outbox WHERE action = 'ruleset.enforced_early'")
            )
            == 1
        )


async def test_a_global_admin_can_enforce_a_platform_set_early(make_stack: Any) -> None:
    async with make_stack(platform_seed=True) as stack:
        response = await stack.client.post(
            platform_url(stack.root_id, REFERENCES_ID), headers=auth(stack.admin_token)
        )

        assert response.status_code == 200, response.text


async def test_any_other_folder_or_a_folder_scoped_set_is_the_uniform_404(make_stack: Any) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        pam, pam_id = stack.token("pam")
        stack.grant_sql(pam_id, "policy_manager", stack.root_id)
        created = await stack.create_rule_set(chain.org, "Folder scoped", token=pam)
        assert created.status_code == 201, created.text
        folder_set: str = created.json()["id"]

        for folder, rule_set in [
            (chain.org, CONSISTENCY_ID),  # not the ROOT folder
            (chain.team, CONSISTENCY_ID),
            (stack.root_id, folder_set),  # not a platform set
            (stack.root_id, str(uuid.uuid4())),  # unknown
        ]:
            response = await stack.client.post(platform_url(folder, rule_set), headers=auth(pam))
            assert response.status_code == 404, (folder, rule_set, response.text)
            assert response.json()["type"] == "urn:dc:problem:not-found"
        unknown_version = await stack.client.post(
            platform_url(stack.root_id, CONSISTENCY_ID, 9), headers=auth(pam)
        )
        assert unknown_version.status_code == 404
        assert (
            stack.validation_sql("SELECT 1 FROM outbox WHERE action = 'ruleset.enforced_early'")
            == []
        )


async def test_a_policy_manager_granted_only_below_root_cannot_enforce_a_platform_set(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        pam, pam_id = stack.token("pam")
        stack.grant_sql(pam_id, "policy_manager", chain.org)
        reader, reader_id = stack.token("reader")
        stack.grant_sql(reader_id, "global_reader", stack.root_id)

        below = await stack.client.post(
            platform_url(stack.root_id, CONSISTENCY_ID), headers=auth(pam)
        )
        read_only = await stack.client.post(
            platform_url(stack.root_id, CONSISTENCY_ID), headers=auth(reader)
        )

        assert below.status_code in (403, 404), below.text
        assert read_only.status_code in (403, 404), read_only.text
        assert (
            stack.validation_sql("SELECT 1 FROM outbox WHERE action = 'ruleset.enforced_early'")
            == []
        )


# -- validate-phase gap fill: SC2 type coherence and SC3 server references through a real run ---


async def test_incoherent_logical_and_physical_types_are_reported_and_coherent_ones_are_not(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)
        clash = contract([prop("id"), prop("n", logicalType="integer", physicalType="VARCHAR(10)")])
        fine = contract([prop("n", logicalType="integer", physicalType="BIGINT")])

        bad = await post_run(stack, chain.team, editor, clash)
        good = await post_run(stack, chain.team, editor, fine)

        found = [
            f for f in of_set(bad, CONSISTENCY) if f["rule_key"] == "logical-physical-coherent"
        ]
        assert len(found) == 1
        assert found[0]["severity"] == "warning"
        assert found[0]["blocking"] is False
        assert found[0]["location"]["pointer"] == "/schema/0/properties/1"
        assert [
            f for f in of_set(good, CONSISTENCY) if f["rule_key"] == "logical-physical-coherent"
        ] == []


async def test_a_server_reference_to_an_undeclared_server_fails_and_a_declared_one_passes(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)
        servers = [{"server": "prod", "type": "postgres"}]
        ghost = contract(
            [prop("id", customProperties=[{"property": "server", "value": "ghost"}])],
            servers=servers,
        )
        declared = contract(
            [prop("id", customProperties=[{"property": "server", "value": "prod"}])],
            servers=servers,
        )

        bad = await post_run(stack, chain.team, editor, ghost)
        good = await post_run(stack, chain.team, editor, declared)

        found = [f for f in of_set(bad, REFERENCES) if f["rule_key"] == "server-reference-exists"]
        assert [(f["severity"], f["blocking"]) for f in found] == [("error", False)]
        assert [
            f for f in of_set(good, REFERENCES) if f["rule_key"] == "server-reference-exists"
        ] == []


# -- a Global Policy Manager reschedules a platform set (quick 261118-vpa) ---------------------


def rescheduled(stack: Any) -> list[dict[str, Any]]:
    return stack.validation_sql(
        "SELECT * FROM outbox WHERE action = 'ruleset.enforcement_rescheduled' ORDER BY id"
    )


async def test_a_root_policy_manager_postpones_enforces_at_once_and_postpones_again(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain, editor = await team_editor(stack)
        pam, pam_id = stack.token("pam")
        stack.grant_sql(pam_id, "policy_manager", stack.root_id)
        document = contract([prop("id", primaryKey=True, required=True)])
        seeded = stack.validation_sql(
            "SELECT enforce_at FROM rule_set_version WHERE rule_set_id = %s", CONSISTENCY_ID
        )[0]["enforce_at"]
        later = datetime.now(UTC) + timedelta(days=60)

        moved = await put_enforce_at(stack, stack.root_id, CONSISTENCY_ID, pam, later.isoformat())

        assert moved.status_code == 200, moved.text
        assert datetime.fromisoformat(moved.json()["enforce_at"]) == later
        (event,) = rescheduled(stack)
        assert str(event["aggregate_id"]) == CONSISTENCY_ID
        assert event["actor"]["id"] == pam_id
        assert event["before"]["enforce_at"] == seeded.isoformat()
        assert event["after"]["previous_enforce_at"] == seeded.isoformat()
        assert (event["after"]["previous_mode"], event["after"]["mode"]) == ("warn", "warn")
        assert event["after"]["paused"] is False
        repeat = await put_enforce_at(stack, stack.root_id, CONSISTENCY_ID, pam, later.isoformat())
        assert repeat.status_code == 200
        assert len(rescheduled(stack)) == 1

        past = await put_enforce_at(
            stack, stack.root_id, CONSISTENCY_ID, pam, "2000-01-01T00:00:00Z"
        )
        assert past.status_code == 200, past.text
        stored = datetime.fromisoformat(past.json()["enforce_at"])
        assert datetime.now(UTC) - stored < timedelta(minutes=1)  # the database now, never earlier
        blocked = await post_run(stack, chain.team, editor, document)
        assert [(f["rule_key"], f["blocking"]) for f in of_set(blocked, CONSISTENCY)] == [
            ("primary-key-required-unique", True)
        ]
        assert len(rescheduled(stack)) == 2
        again = await put_enforce_at(
            stack, stack.root_id, CONSISTENCY_ID, pam, "2001-01-01T00:00:00Z"
        )
        assert again.status_code == 200
        assert len(rescheduled(stack)) == 2  # already enforced: a past date is a no-op

        postponed = await put_enforce_at(
            stack, stack.root_id, CONSISTENCY_ID, pam, later.isoformat()
        )
        assert postponed.status_code == 200
        warned = await post_run(stack, chain.team, editor, document)
        assert [(f["rule_key"], f["blocking"]) for f in of_set(warned, CONSISTENCY)] == [
            ("primary-key-required-unique", False)
        ]
        assert len(rescheduled(stack)) == 3


async def test_platform_reschedule_refusals_are_uniform(make_stack: Any) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        pam, pam_id = stack.token("pam")
        stack.grant_sql(pam_id, "policy_manager", stack.root_id)
        below, below_id = stack.token("below")
        stack.grant_sql(below_id, "policy_manager", chain.org)
        created = await stack.create_rule_set(chain.org, "Folder scoped", token=pam)
        assert created.status_code == 201, created.text
        folder_set: str = created.json()["id"]
        when = "2030-01-01T00:00:00Z"

        for folder, rule_set in [
            (chain.org, CONSISTENCY_ID),  # not the ROOT folder
            (stack.root_id, folder_set),  # not a platform set
            (stack.root_id, str(uuid.uuid4())),  # unknown
        ]:
            response = await put_enforce_at(stack, folder, rule_set, pam, when)
            assert response.status_code == 404, (folder, rule_set, response.text)
            assert response.json()["type"] == "urn:dc:problem:not-found"
        assert (
            await put_enforce_at(stack, stack.root_id, CONSISTENCY_ID, pam, when, 9)
        ).status_code == 404
        refused = await put_enforce_at(stack, stack.root_id, CONSISTENCY_ID, below, when)
        assert refused.status_code in (403, 404), refused.text
        anonymous = await stack.client.put(
            enforce_at_url(stack.root_id, CONSISTENCY_ID), json={"enforce_at": when}
        )
        assert anonymous.status_code == 401
        naive = await put_enforce_at(
            stack, stack.root_id, CONSISTENCY_ID, pam, "2030-01-01T00:00:00"
        )
        assert naive.status_code == 422
        extra = await stack.client.put(
            enforce_at_url(stack.root_id, CONSISTENCY_ID),
            json={"enforce_at": when, "x": 1},
            headers=auth(pam),
        )
        assert extra.status_code == 422
        assert rescheduled(stack) == []
