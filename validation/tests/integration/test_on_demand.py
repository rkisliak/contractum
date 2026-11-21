"""Validate an uploaded contract on demand over HTTP (plan 05-12; VAL-08, D-126, D-127, D-137).

``POST /api/v1/validation/folders/{folder_id}/validate`` runs the same pipeline as the internal
run route in save mode: the same report and pins, and nothing is stored. The Plan 07 stack runs
identity and validation in one process; the helpers below are small copies of the ones in
``test_runs.py`` (test modules cannot import each other under importlib mode).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from typing import Any

import pytest
import yaml
from dc_odcs import ALLOWED_MEDIA_TYPES, schema_manifest_sha256

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

BASE = "/api/v1/validation"
UNIQUE_NAMES = "this.properties.map(p, p.name).distinct().size() == this.properties.size()"
YAML = "application/yaml"
JSON = "application/json"


def table(*names: str) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": n} for n in names]}]}


def rule_body(message: str = "Property names must be unique.", *, severity: str = "error") -> str:
    return json.dumps(
        {
            "target": "$.schema[*]",
            "condition": UNIQUE_NAMES,
            "severity": severity,
            "message": message,
            "fixtures": {
                "pass": [{"document": table("id", "name")}],
                "fail": [{"document": table("id", "id")}],
            },
        }
    )


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def contract(*names: str, **extra: Any) -> dict[str, Any]:
    """A conformant ODCS v3.2.0 document with one table holding the named properties."""
    document: dict[str, Any] = {
        "kind": "DataContract",
        "apiVersion": "v3.2.0",
        "id": "urn:datacontract:test:orders",
        "version": "1.0.0",
        "status": "draft",
        "schema": [
            {
                "name": "orders",
                "properties": [{"name": n, "logicalType": "string"} for n in names],
            }
        ],
    }
    document.update(extra)
    return document


def as_yaml(document: dict[str, Any]) -> bytes:
    return yaml.safe_dump(document, sort_keys=False).encode()


async def setup(stack: Any) -> tuple[Any, str, str, str]:
    """A chain with a Policy Manager at org A and a team editor at team T (below A)."""
    chain = await stack.chain()
    pam, pam_id = stack.token("pam")
    stack.grant_sql(pam_id, "policy_manager", chain.org)
    ted, ted_id = stack.token("ed")
    stack.grant_sql(ted_id, "team_editor", chain.team)
    return chain, pam, ted, ted_id


async def new_set(
    stack: Any,
    folder: str,
    token: str,
    *,
    name: str = "Org naming",
    body: str | None = None,
    versions: int = 1,
) -> str:
    """Create a rule set and publish ``versions`` versions of one rule."""
    created = await stack.create_rule_set(folder, name, token=token)
    assert created.status_code == 201, created.text
    rule_set_id: str = created.json()["id"]
    for number in range(1, versions + 1):
        current = await stack.get(f"{BASE}/rule-sets/{rule_set_id}", token=token)
        message = "Property names must be unique."
        if versions > 1:
            message = f"Property names v{number} must be unique."
        put = await stack.put_rule(
            rule_set_id,
            "unique-property-names",
            body or rule_body(message),
            current.headers["ETag"],
            token=token,
        )
        assert put.status_code == 200, put.text
        published = await stack.publish(rule_set_id, put.headers["ETag"], token=token)
        assert published.status_code == 201, published.text
    return rule_set_id


async def mandatory_enforced(stack: Any, folder: str, token: str, **kwargs: Any) -> str:
    rule_set_id = await new_set(stack, folder, token, **kwargs)
    mandated = await stack.client.put(
        f"{BASE}/rule-sets/{rule_set_id}/mandatory", json={"mandatory": True}, headers=auth(token)
    )
    assert mandated.status_code == 200, mandated.text
    enforced = await stack.client.post(
        f"{BASE}/rule-sets/{rule_set_id}/versions/1/enforce-now", headers=auth(token)
    )
    assert enforced.status_code == 200, enforced.text
    return rule_set_id


def validate_url(folder_id: str) -> str:
    return f"{BASE}/folders/{folder_id}/validate"


async def upload(
    stack: Any,
    folder_id: str,
    token: str | None,
    document: bytes,
    *,
    media_type: str = YAML,
    rule_sets: list[str] | None = None,
) -> Any:
    headers = {**(auth(token) if token else {}), "Content-Type": media_type}
    params = [("rule_set", value) for value in rule_sets or []]
    return await stack.client.post(
        validate_url(folder_id), content=document, headers=headers, params=params
    )


def stored_runs(stack: Any) -> int:
    return int(stack.validation_sql("SELECT count(*) AS n FROM validation_run")[0]["n"])


def outbox_rows(stack: Any) -> int:
    return int(stack.validation_sql("SELECT count(*) AS n FROM outbox")[0]["n"])


def findings_of(body: dict[str, Any], rule_set_name: str) -> list[dict[str, Any]]:
    return [f for f in body["report"]["findings"] if f["rule_set"]["name"] == rule_set_name]


# -- tracer ---------------------------------------------------------------------------------------


async def test_a_team_editor_uploads_a_contract_and_gets_the_report_without_anything_saved(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, ted, _ = await setup(stack)
        await mandatory_enforced(stack, chain.org, pam)
        runs_before, outbox_before = stored_runs(stack), outbox_rows(stack)
        broken = as_yaml(contract("id", "id"))

        response = await upload(stack, chain.team, ted, broken)

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {
            "content_sha256",
            "engine_version",
            "dsl_version",
            "schema_sha",
            "pins",
            "report",
        }
        assert body["content_sha256"] == hashlib.sha256(broken).hexdigest()
        assert body["engine_version"]
        assert body["dsl_version"] == "cel-2"
        assert body["schema_sha"] == schema_manifest_sha256()
        assert [p["source"] for p in body["pins"] if p["name"] == "Org naming"] == ["mandatory"]
        assert body["report"]["summary"]["publishable"] is False
        found = findings_of(body, "Org naming")
        assert len(found) == 1
        assert (found[0]["rule_key"], found[0]["outcome"], found[0]["blocking"]) == (
            "unique-property-names",
            "fail",
            True,
        )
        assert found[0]["location"]["pointer"] == "/schema/0"

        fixed = await upload(stack, chain.team, ted, as_yaml(contract("id", "name")))
        assert fixed.status_code == 200, fixed.text
        assert fixed.json()["report"]["summary"]["publishable"] is True

        as_json = await upload(
            stack, chain.team, ted, json.dumps(contract("id", "id")).encode(), media_type=JSON
        )
        assert as_json.status_code == 200, as_json.text
        assert as_json.json()["report"]["summary"] == body["report"]["summary"]

        assert stored_runs(stack) == runs_before
        assert outbox_rows(stack) == outbox_before


async def test_the_upload_route_needs_a_bearer_the_permission_and_a_known_folder(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, _, ted, _ = await setup(stack)
        reader, reader_id = stack.token("consumer")
        stack.grant_sql(reader_id, "consumer", chain.team)
        document = as_yaml(contract("id", "name"))

        assert (await upload(stack, chain.team, None, document)).status_code == 401
        assert (await upload(stack, chain.team, reader, document)).status_code in (403, 404)
        unknown = "00000000-0000-4000-8000-000000000000"
        assert (await upload(stack, unknown, ted, document)).status_code == 404
        assert (await upload(stack, chain.team, ted, document)).status_code == 200


async def test_media_type_size_and_syntax_problems_are_refused_without_a_run(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, _, ted, _ = await setup(stack)
        document = as_yaml(contract("id", "name"))

        other = await upload(stack, chain.team, ted, document, media_type="text/plain")
        assert other.status_code == 415
        assert other.json()["type"] == "urn:dc:problem:unsupported-media-type"

        huge = as_yaml(contract("id", note="x" * 1_100_000))
        too_big = await upload(stack, chain.team, ted, huge)
        assert too_big.status_code == 413
        assert too_big.json()["type"] == "urn:dc:problem:payload-too-large"

        malformed = await upload(stack, chain.team, ted, b"a: [unclosed")
        assert malformed.status_code == 422
        assert malformed.json()["type"] == "urn:dc:problem:malformed-document"
        assert stored_runs(stack) == 0


async def test_the_upload_route_is_in_openapi_with_both_request_media_types(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        response = await stack.client.get(f"{BASE}/openapi.json")
        assert response.status_code == 200
        operation = response.json()["paths"][f"{BASE}/folders/{{folder_id}}/validate"]["post"]
        content = set(operation["requestBody"]["content"])
        assert {YAML, JSON} <= content
        assert set(ALLOWED_MEDIA_TYPES) <= content


# -- plan 05-12 task 2: ad-hoc rule sets, Policy Manager uploads and saturation -----------------


async def test_an_adhoc_rule_set_adds_findings_that_count_toward_publishable(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, _, ted, _ = await setup(stack)
        admin = stack.admin_token
        root_set = await new_set(stack, stack.root_id, admin, name="Root errors")
        document = as_yaml(contract("id", "id"))

        plain = (await upload(stack, chain.team, ted, document)).json()
        assert findings_of(plain, "Root errors") == []
        assert plain["report"]["summary"]["publishable"] is True

        response = await upload(stack, chain.team, ted, document, rule_sets=[root_set])

        assert response.status_code == 200, response.text
        body = response.json()
        found = findings_of(body, "Root errors")
        assert [(f["source"], f["outcome"], f["blocking"]) for f in found] == [
            ("adhoc", "fail", True)
        ]
        assert body["report"]["summary"]["publishable"] is False
        assert [p["source"] for p in body["pins"] if p["name"] == "Root errors"] == ["adhoc"]
        assert stored_runs(stack) == 0


async def test_an_adhoc_exact_version_resolves_even_when_deprecated(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, _, ted, _ = await setup(stack)
        admin = stack.admin_token
        set_id = await new_set(stack, stack.root_id, admin, name="Two versions", versions=2)
        deprecated = await stack.client.post(
            f"{BASE}/rule-sets/{set_id}/versions/1/deprecate", headers=auth(admin)
        )
        assert deprecated.status_code == 200, deprecated.text
        document = as_yaml(contract("id", "id"))

        exact = (await upload(stack, chain.team, ted, document, rule_sets=[f"{set_id}@1"])).json()
        latest = (await upload(stack, chain.team, ted, document, rule_sets=[set_id])).json()

        assert [
            (p["version"], p["deprecated_version"], p["source"])
            for p in exact["pins"]
            if p["name"] == "Two versions"
        ] == [(1, True, "adhoc")]
        assert [
            (p["version"], p["deprecated_version"])
            for p in latest["pins"]
            if p["name"] == "Two versions"
        ] == [(2, False)]


async def test_an_adhoc_set_that_is_already_mandatory_resolves_once_as_mandatory(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, ted, _ = await setup(stack)
        set_id = await mandatory_enforced(stack, chain.org, pam)

        body = (
            await upload(
                stack,
                chain.team,
                ted,
                as_yaml(contract("id", "id")),
                rule_sets=[set_id, f"{set_id}@1"],
            )
        ).json()

        assert [p["source"] for p in body["pins"] if p["name"] == "Org naming"] == ["mandatory"]
        assert [f["source"] for f in findings_of(body, "Org naming")] == ["mandatory"]


async def test_an_adhoc_set_that_cannot_resolve_is_a_422_naming_the_parameter(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, _, ted, _ = await setup(stack)
        other = await stack.chain()
        sibling = await new_set(stack, other.org, stack.admin_token, name="Sibling only")
        root_set = await new_set(stack, stack.root_id, stack.admin_token, name="Root set")
        unknown = "00000000-0000-4000-8000-000000000001"
        document = as_yaml(contract("id", "name"))

        for value in (sibling, unknown, f"{root_set}@9", f"{sibling}@1"):
            response = await upload(stack, chain.team, ted, document, rule_sets=[value])
            assert response.status_code == 422, (value, response.text)
            problem = response.json()
            assert problem["type"] == "urn:dc:problem:rule-set-not-attachable"
            assert value in problem["detail"]
            # The same fixed text whatever the reason: a sibling's set and an unknown id look alike.
            assert problem["detail"] == (
                f"Rule set {value} is not a published rule set visible at this folder."
            )
        assert stored_runs(stack) == 0


async def test_malformed_and_too_many_rule_set_parameters_are_validation_errors(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, _, ted, _ = await setup(stack)
        root_set = await new_set(stack, stack.root_id, stack.admin_token, name="Root set")
        document = as_yaml(contract("id", "name"))

        for value in (
            "not-a-uuid",
            f"{root_set}@",
            f"{root_set}@0",
            f"{root_set}@x",
            f"{root_set}@1@2",
        ):
            response = await upload(stack, chain.team, ted, document, rule_sets=[value])
            assert response.status_code == 422, (value, response.text)
            assert response.json()["type"] == "urn:dc:problem:validation-error"

        eleven = [root_set] * 11
        too_many = await upload(stack, chain.team, ted, document, rule_sets=eleven)
        assert too_many.status_code == 422
        assert too_many.json()["type"] == "urn:dc:problem:validation-error"
        ten = await upload(stack, chain.team, ted, document, rule_sets=[root_set] * 10)
        assert ten.status_code == 200, ten.text


async def test_a_policy_manager_validates_a_document_they_upload_and_a_consumer_cannot(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, _, _ = await setup(stack)
        reader, reader_id = stack.token("consumer2")
        stack.grant_sql(reader_id, "consumer", chain.team)
        document = as_yaml(contract("id", "name"))

        as_manager = await upload(stack, chain.team, pam, document)
        assert as_manager.status_code == 200, as_manager.text
        assert as_manager.json()["report"]["summary"]["publishable"] is True

        denied = await upload(stack, chain.team, reader, document)
        assert denied.status_code in (403, 404)
        assert stored_runs(stack) == 0


async def test_saturation_answers_503_and_concurrent_uploads_never_cross_talk(
    make_stack: Any,
) -> None:
    async with make_stack(max_concurrent_runs=2, queue_wait_s=0.2) as stack:
        chain, pam, ted, _ = await setup(stack)
        await mandatory_enforced(stack, chain.org, pam)  # a rule must exist to use the sandbox
        runs_before, outbox_before = stored_runs(stack), outbox_rows(stack)
        sandbox = stack.app.state.sandbox

        async with sandbox.slot(), sandbox.slot():
            refused = await asyncio.gather(
                *(
                    upload(stack, chain.team, ted, as_yaml(contract("id", f"c{i}")))
                    for i in range(3)
                )
            )
        for response in refused:
            assert response.status_code == 503, response.text
            assert response.headers["Retry-After"] == "1"
            assert response.json()["type"] == "urn:dc:problem:validation-unavailable"
        assert stored_runs(stack) == runs_before

        stack.app.state.settings.queue_wait_s = 10.0
        documents = [as_yaml(contract("id", f"column_{i}")) for i in range(6)]
        answered = await asyncio.gather(*(upload(stack, chain.team, ted, d) for d in documents))
        for document, response in zip(documents, answered, strict=True):
            assert response.status_code == 200, response.text
            assert response.json()["content_sha256"] == hashlib.sha256(document).hexdigest()
            assert response.json()["report"]["summary"]["publishable"] is True
        assert stored_runs(stack) == runs_before
        assert outbox_rows(stack) == outbox_before


async def test_a_request_cancelled_while_waiting_for_a_slot_leaves_no_stuck_slot(
    make_stack: Any,
) -> None:
    async with make_stack(max_concurrent_runs=1, queue_wait_s=0.5) as stack:
        chain, pam, ted, _ = await setup(stack)
        await mandatory_enforced(stack, chain.org, pam)
        document = as_yaml(contract("id", "name"))
        sandbox = stack.app.state.sandbox
        stack.app.state.settings.queue_wait_s = 10.0

        async with sandbox.slot():
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(upload(stack, chain.team, ted, document), 0.05)

        after = await upload(stack, chain.team, ted, document)
        assert after.status_code == 200, after.text

        # Cancelled at assorted points of a normal run: whichever point it hit, the slot is back.
        for timeout in (0.001, 0.01, 0.03, 0.08):
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(upload(stack, chain.team, ted, document), timeout)
        stack.app.state.settings.queue_wait_s = 0.5
        final = await upload(stack, chain.team, ted, document)
        assert final.status_code == 200, final.text
        assert stored_runs(stack) == 0


# -- plan 06-06 task 2: the previous document ------------------------------------------

PAIR = "application/vnd.dc.validate-pair+json"


def pair(document: dict[str, Any], previous: dict[str, Any] | bytes) -> bytes:
    return json.dumps(
        {
            "document": as_yaml(document).decode(),
            "document_media_type": YAML,
            "previous_document": (
                previous.decode() if isinstance(previous, bytes) else as_yaml(previous).decode()
            ),
            "previous_media_type": YAML,
        }
    ).encode()


def native(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in body["report"]["findings"] if f["source"] == "native"]


async def test_a_pair_upload_reports_compatibility_against_the_previous_document(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, _pam, ted, _ = await setup(stack)
        previous = contract("id", "amount", version="1.0.0")

        breaking = await upload(
            stack, chain.team, ted, pair(contract("id", version="1.1.0"), previous), media_type=PAIR
        )
        major = await upload(
            stack, chain.team, ted, pair(contract("id", version="2.0.0"), previous), media_type=PAIR
        )
        prerelease = await upload(
            stack,
            chain.team,
            ted,
            pair(contract("id", version="1.1.0"), contract("id", "amount", version="1.0.0-rc.1")),
            media_type=PAIR,
        )
        plain = await upload(stack, chain.team, ted, as_yaml(contract("id")))

        assert breaking.status_code == 200, breaking.text
        compat = breaking.json()["report"]["compatibility"]
        assert compat["status"] == "compared"
        assert compat["required_bump"] == "major"
        assert compat["bump_ok"] is False
        assert compat["baseline"] == {"version_id": None, "semver": "1.0.0"}
        assert compat["changes"][0]["code"] == "property-removed"
        (finding,) = native(breaking.json())
        assert finding["rule_key"] == "semver-breaking-change"
        assert breaking.json()["report"]["summary"]["publishable"] is False
        assert major.json()["report"]["compatibility"]["bump_ok"] is True
        assert native(major.json()) == []
        assert native(prerelease.json()) == []
        assert prerelease.json()["report"]["compatibility"]["counts"]["breaking"] == 1
        assert plain.status_code == 200, plain.text
        assert plain.json()["report"]["compatibility"]["status"] == "no-baseline"
        assert stored_runs(stack) == 0


async def test_an_unusable_previous_document_is_advisory_and_a_document_over_the_cap_is_413(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, _pam, ted, _ = await setup(stack)

        unparseable = await upload(
            stack, chain.team, ted, pair(contract("id"), b"a: [unclosed"), media_type=PAIR
        )
        big_previous = await upload(
            stack, chain.team, ted, pair(contract("id"), b"a: " + b"x" * 1_100_000), media_type=PAIR
        )
        big_document = await upload(
            stack,
            chain.team,
            ted,
            pair(contract("id", note="x" * 1_100_000), contract("id")),  # type: ignore[arg-type]
            media_type=PAIR,
        )

        assert unparseable.status_code == 200, unparseable.text
        assert unparseable.json()["report"]["compatibility"]["status"] == "unavailable"
        assert native(unparseable.json()) == []
        assert big_previous.status_code == 413
        assert big_previous.json()["type"] == "urn:dc:problem:payload-too-large"
        assert big_document.status_code == 413


async def test_a_malformed_pair_envelope_is_a_422_with_a_fixed_text(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, _pam, ted, _ = await setup(stack)
        good = json.loads(pair(contract("id"), contract("id")))
        missing = {k: v for k, v in good.items() if k != "previous_document"}
        extra = {**good, "unexpected": 1}
        wrong_type = {**good, "document": 7}

        for body in (
            b"{not json",
            json.dumps(missing).encode(),
            json.dumps(extra).encode(),
            json.dumps(wrong_type).encode(),
            b"[]",
        ):
            response = await upload(stack, chain.team, ted, body, media_type=PAIR)
            assert response.status_code == 422, response.text
            assert response.json()["type"] == "urn:dc:problem:validation-error"
            assert response.json()["detail"] == "The request did not validate."
        assert "not json" not in response.text


async def test_the_upload_route_documents_the_pair_media_type(make_stack: Any) -> None:
    async with make_stack() as stack:
        response = await stack.client.get(f"{BASE}/openapi.json")
        operation = response.json()["paths"][f"{BASE}/folders/{{folder_id}}/validate"]["post"]
        assert set(operation["requestBody"]["content"]) == set(ALLOWED_MEDIA_TYPES) | {PAIR}
