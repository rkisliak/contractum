"""The internal validation run route over HTTP (plan 05-11; VAL-01, VAL-07, RULE-09, D-125..D-133).

The Plan 07 stack runs identity and validation in one process. A test posts to the internal route
with a real fake-issuer bearer, exactly as contracts will with the caller's own forwarded token.
Assertions are on the findings and pins of the sets a test creates, never on totals: the platform
structural set arrives in a later plan.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from typing import Any

import pytest
import yaml
from dc_odcs import schema_manifest_sha256
from dc_validation.api.runs import run_body_cap

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

BASE = "/api/v1/validation"
UNIQUE_NAMES = "this.properties.map(p, p.name).distinct().size() == this.properties.size()"


def table(*names: str) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": n} for n in names]}]}


def rule_body(
    message: str = "Property names must be unique.",
    *,
    condition: str = UNIQUE_NAMES,
    severity: str = "error",
    target: str = "$.schema[*]",
) -> str:
    return json.dumps(
        {
            "target": target,
            "condition": condition,
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


async def publish_next(
    stack: Any,
    rule_set_id: str,
    token: str,
    body: str | None = None,
    key: str = "unique-property-names",
    *,
    rules: dict[str, str] | None = None,
) -> None:
    """Put each rule into the draft, then publish the draft once as the next version."""
    current = await stack.get(f"{BASE}/rule-sets/{rule_set_id}", token=token)
    etag = current.headers["ETag"]
    for rule_key, rule in (rules or {key: body or rule_body()}).items():
        put = await stack.put_rule(rule_set_id, rule_key, rule, etag, token=token)
        assert put.status_code == 200, put.text
        etag = put.headers["ETag"]
    published = await stack.publish(rule_set_id, etag, token=token)
    assert published.status_code == 201, published.text


async def new_set(
    stack: Any,
    folder: str,
    token: str,
    *,
    name: str = "Org naming",
    rules: dict[str, str] | None = None,
    versions: int = 1,
) -> str:
    """Create a rule set and publish ``versions`` versions, each holding all of ``rules``."""
    created = await stack.create_rule_set(folder, name, token=token)
    assert created.status_code == 201, created.text
    rule_set_id: str = created.json()["id"]
    for number in range(1, versions + 1):
        bodies = dict(rules or {"unique-property-names": rule_body()})
        if versions > 1:
            bodies = {
                k: v.replace("Property names", f"Property names v{number}")
                for k, v in bodies.items()
            }
        await publish_next(stack, rule_set_id, token, rules=bodies)
    return rule_set_id


async def mandate(stack: Any, rule_set_id: str, token: str, value: bool = True) -> None:
    response = await stack.client.put(
        f"{BASE}/rule-sets/{rule_set_id}/mandatory", json={"mandatory": value}, headers=auth(token)
    )
    assert response.status_code == 200, response.text


async def enforce_now(stack: Any, rule_set_id: str, version_no: int, token: str) -> None:
    response = await stack.client.post(
        f"{BASE}/rule-sets/{rule_set_id}/versions/{version_no}/enforce-now", headers=auth(token)
    )
    assert response.status_code == 200, response.text


async def mandatory_enforced(stack: Any, folder: str, token: str, **kwargs: Any) -> str:
    rule_set_id = await new_set(stack, folder, token, **kwargs)
    await mandate(stack, rule_set_id, token)
    await enforce_now(stack, rule_set_id, 1, token)
    return rule_set_id


def run_url(folder_id: str) -> str:
    return f"/internal/v1/validation/folders/{folder_id}/runs"


def run_payload(
    document: bytes | str,
    *,
    mode: str = "publish",
    media_type: str = "application/yaml",
    attachments: list[dict[str, Any]] | None = None,
    attachments_revision: int | None = 0,
    with_ids: bool = True,
) -> dict[str, Any]:
    raw = document.encode() if isinstance(document, str) else document
    payload: dict[str, Any] = {
        "mode": mode,
        "media_type": media_type,
        "document_b64": base64.b64encode(raw).decode(),
        "attachments": attachments or [],
        "attachments_revision": attachments_revision,
    }
    if with_ids:
        payload["contract_id"] = str(uuid.uuid4())
        payload["draft_id"] = str(uuid.uuid4())
    return payload


async def post_run(
    stack: Any, folder_id: str, token: str | None, payload: dict[str, Any] | bytes
) -> Any:
    headers = auth(token) if token else {}
    if isinstance(payload, bytes):
        return await stack.client.post(
            run_url(folder_id),
            content=payload,
            headers={**headers, "Content-Type": "application/json"},
        )
    return await stack.client.post(run_url(folder_id), json=payload, headers=headers)


def runs(stack: Any) -> list[dict[str, Any]]:
    return stack.validation_sql("SELECT * FROM validation_run ORDER BY id")


def findings_of(body: dict[str, Any], rule_set_name: str) -> list[dict[str, Any]]:
    return [f for f in body["report"]["findings"] if f["rule_set"]["name"] == rule_set_name]


# -- tracer ---------------------------------------------------------------------------------------


async def test_a_publish_run_that_breaks_an_enforced_mandatory_rule_is_blocked_and_stored(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, ed, ed_id = await setup(stack)
        set_id = await mandatory_enforced(stack, chain.org, pam)
        document = as_yaml(contract("id", "id"))

        response = await post_run(stack, chain.team, ed, run_payload(document))

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["run_id"] is not None
        assert body["mode"] == "publish"
        assert body["content_sha256"] == hashlib.sha256(document).hexdigest()
        assert body["engine_version"]
        assert body["dsl_version"] == "cel-2"
        assert body["schema_sha"] == schema_manifest_sha256()
        assert body["attachments_revision"] == 0
        pins = [p for p in body["pins"] if p["name"] == "Org naming"]
        assert pins == [
            {
                "rule_set_id": set_id,
                "name": "Org naming",
                "folder_id": chain.org,
                "scope": "folder",
                "version": 1,
                "source": "mandatory",
                "mode": "enforce",
                "dsl_version": "cel-2",
                "deprecated_version": False,
                "paused": False,
            }
        ]
        assert body["report"]["summary"]["publishable"] is False
        assert body["report"]["summary"]["blocking_count"] >= 1
        found = findings_of(body, "Org naming")
        assert len(found) == 1
        assert found[0]["rule_key"] == "unique-property-names"
        assert found[0]["outcome"] == "fail"
        assert found[0]["blocking"] is True
        assert found[0]["location"]["pointer"] == "/schema/0"

        stored = runs(stack)
        assert len(stored) == 1
        assert str(stored[0]["id"]) == body["run_id"]
        assert stored[0]["publishable"] is False
        assert stored[0]["requested_by"] == ed_id
        assert stored[0]["content_sha256"] == body["content_sha256"]
        assert str(stored[0]["folder_id"]) == chain.team

        clean = await post_run(stack, chain.team, ed, run_payload(as_yaml(contract("id", "name"))))
        assert clean.status_code == 200, clean.text
        assert clean.json()["report"]["summary"]["publishable"] is True
        assert clean.json()["run_id"] != body["run_id"]
        assert len(runs(stack)) == 2

        saved = await post_run(
            stack, chain.team, ed, run_payload(document, mode="save", with_ids=False)
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["run_id"] is None
        assert saved.json()["report"]["summary"]["publishable"] is False
        assert len(runs(stack)) == 2


async def test_the_route_needs_a_bearer_the_contract_validate_permission_and_a_known_folder(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, _, ed, _ = await setup(stack)
        reader, reader_id = stack.token("consumer")
        stack.grant_sql(reader_id, "consumer", chain.team)
        payload = run_payload(as_yaml(contract("id", "name")))

        assert (await post_run(stack, chain.team, None, payload)).status_code == 401
        assert (await post_run(stack, chain.team, reader, payload)).status_code in (403, 404)
        assert (await post_run(stack, str(uuid.uuid4()), ed, payload)).status_code == 404
        assert (await post_run(stack, chain.team, ed, payload)).status_code == 200
        assert len(runs(stack)) == 1  # only the allowed call stored a run


async def test_the_internal_route_is_not_in_openapi(make_stack: Any) -> None:
    async with make_stack() as stack:
        response = await stack.client.get("/api/v1/validation/openapi.json")
        assert response.status_code == 200
        assert not [path for path in response.json()["paths"] if "/internal" in path]


# -- plan 05-11 task 2: resolution, blocking, fail closed and value-free ----------------

SECRET = "SECRET-VALUE-123"
SECRET_VERSION = "SECRET-VER-7"
SECRET_KEY_NAME = "SECRET-KEY-NAME-9"


def custom_rule(
    condition: str,
    pass_document: dict[str, Any],
    fail_document: dict[str, Any],
    message: str,
    *,
    severity: str = "error",
    target: str = "$.schema[*]",
    pass_ctx: dict[str, Any] | None = None,
    fail_ctx: dict[str, Any] | None = None,
) -> str:
    def fixture(document: dict[str, Any], ctx: dict[str, Any] | None) -> dict[str, Any]:
        entry: dict[str, Any] = {"document": document}
        if ctx is not None:
            entry["ctx"] = ctx
        return entry

    return json.dumps(
        {
            "target": target,
            "condition": condition,
            "severity": severity,
            "message": message,
            "fixtures": {
                "pass": [fixture(pass_document, pass_ctx)],
                "fail": [fixture(fail_document, fail_ctx)],
            },
        }
    )


OWNER_RULE = custom_rule(
    'this.owner == "platform"',
    {"schema": [{"owner": "platform"}]},
    {"schema": [{"owner": "other"}]},
    "Every table needs an owner.",
)


def structural_codes(body: dict[str, Any]) -> list[str]:
    return [e["keyword"] for e in body["report"]["structural"]["errors"]]


async def test_enforced_and_warn_versions_of_one_set_are_each_reported_once(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, ed, _ = await setup(stack)
        set_id = await mandatory_enforced(stack, chain.org, pam)
        await publish_next(
            stack,
            set_id,
            pam,
            rule_body("Property names v2 must be unique."),
            "unique-property-names",
        )

        response = await post_run(stack, chain.team, ed, run_payload(as_yaml(contract("id", "id"))))

        assert response.status_code == 200, response.text
        body = response.json()
        found = findings_of(body, "Org naming")
        assert len(found) == 2
        by_version = {f["rule_set"]["version"]: f for f in found}
        assert (by_version[1]["mode"], by_version[1]["blocking"]) == ("enforce", True)
        assert by_version[1]["enforce_at"] is None
        assert (by_version[2]["mode"], by_version[2]["blocking"]) == ("warn", False)
        assert by_version[2]["enforce_at"] is not None
        assert by_version[2]["message"] == "Property names v2 must be unique."
        assert body["report"]["summary"]["publishable"] is False
        assert sorted(
            (p["version"], p["mode"]) for p in body["pins"] if p["name"] == "Org naming"
        ) == [
            (1, "enforce"),
            (2, "warn"),
        ]


async def test_an_evaluation_error_blocks_only_where_a_failure_would_and_the_rest_still_runs(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, ed, _ = await setup(stack)
        rules = {"owner-set": OWNER_RULE, "unique-property-names": rule_body()}
        enforced = await mandatory_enforced(stack, chain.org, pam, name="Enforced", rules=rules)
        warn_id = await new_set(stack, chain.org, pam, name="Warned", rules=rules)
        await mandate(stack, warn_id, pam)  # warn only: the grace period has not passed
        document = as_yaml(contract("id", "name"))  # no `owner` on the table: no_such_key

        body = (await post_run(stack, chain.team, ed, run_payload(document))).json()

        errors = [f for f in body["report"]["findings"] if f["outcome"] == "error"]
        enforced_error = next(f for f in errors if f["rule_set"]["name"] == "Enforced")
        warn_error = next(f for f in errors if f["rule_set"]["name"] == "Warned")
        assert enforced_error["error_code"] == "no_such_key"
        assert enforced_error["rule_key"] == "owner-set"
        assert enforced_error["blocking"] is True
        assert warn_error["error_code"] == "no_such_key"
        assert warn_error["blocking"] is False
        assert body["report"]["summary"]["blocking_count"] == 1
        assert body["report"]["summary"]["publishable"] is False
        counts = {c["rule_set_id"]: c for c in body["report"]["rule_sets"]}
        assert (counts[enforced]["passed"], counts[enforced]["errored"]) == (1, 1)
        assert (counts[warn_id]["passed"], counts[warn_id]["errored"]) == (1, 1)


async def test_an_error_in_a_warn_only_set_leaves_the_contract_publishable(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam, ed, _ = await setup(stack)
        warn_id = await new_set(stack, chain.org, pam, rules={"owner-set": OWNER_RULE})
        await mandate(stack, warn_id, pam)

        body = (
            await post_run(stack, chain.team, ed, run_payload(as_yaml(contract("id", "name"))))
        ).json()

        found = findings_of(body, "Org naming")
        assert [(f["outcome"], f["blocking"], f["mode"]) for f in found] == [
            ("error", False, "warn")
        ]
        assert body["report"]["summary"] == {"publishable": True, "blocking_count": 0}


async def test_explicit_attachments_block_on_error_rules_and_not_on_warnings(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, _, ed, _ = await setup(stack)
        admin = stack.admin_token
        errors_id = await new_set(stack, stack.root_id, admin, name="Root errors")
        advice_id = await new_set(
            stack,
            stack.root_id,
            admin,
            name="Root advice",
            rules={"unique-property-names": rule_body(severity="warning")},
        )
        document = as_yaml(contract("id", "id"))

        blocked = (
            await post_run(
                stack,
                chain.team,
                ed,
                run_payload(
                    document,
                    attachments=[{"rule_set_id": errors_id, "version": "latest"}],
                ),
            )
        ).json()
        found = findings_of(blocked, "Root errors")
        assert [(f["source"], f["mode"], f["blocking"]) for f in found] == [
            ("explicit", "enforce", True)
        ]
        assert blocked["report"]["summary"]["publishable"] is False

        advised = (
            await post_run(
                stack,
                chain.team,
                ed,
                run_payload(document, attachments=[{"rule_set_id": advice_id, "version": 1}]),
            )
        ).json()
        found = findings_of(advised, "Root advice")
        assert [(f["source"], f["severity"], f["blocking"]) for f in found] == [
            ("explicit", "warning", False)
        ]
        assert advised["report"]["summary"] == {"publishable": True, "blocking_count": 0}
        assert [p["source"] for p in advised["pins"] if p["name"] == "Root advice"] == ["explicit"]


async def test_an_attachment_that_cannot_resolve_is_one_blocking_finding_never_skipped(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, _, ed, _ = await setup(stack)
        other = await stack.chain()
        sibling_id = await new_set(stack, other.org, stack.admin_token, name="Sibling only")
        root_id = await new_set(stack, stack.root_id, stack.admin_token, name="Root set")
        document = as_yaml(contract("id", "name"))  # clean, so only the attachment can block

        for attachment in (
            {"rule_set_id": sibling_id, "version": "latest"},
            {"rule_set_id": str(uuid.uuid4()), "version": "latest"},
            {"rule_set_id": root_id, "version": 9},
        ):
            body = (
                await post_run(
                    stack, chain.team, ed, run_payload(document, attachments=[attachment])
                )
            ).json()
            unresolved = [
                f for f in body["report"]["findings"] if f["error_code"] == "unresolvable_rule_set"
            ]
            assert len(unresolved) == 1, attachment
            assert unresolved[0]["blocking"] is True
            assert unresolved[0]["outcome"] == "error"
            assert unresolved[0]["rule_set"]["id"] == attachment["rule_set_id"]
            assert body["report"]["summary"]["publishable"] is False
            assert not [p for p in body["pins"] if p["name"] in ("Sibling only", "Root set")]


async def test_latest_skips_deprecated_versions_and_an_exact_version_does_not(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, _, ed, _ = await setup(stack)
        admin = stack.admin_token
        set_id = await new_set(stack, stack.root_id, admin, name="Two versions", versions=2)
        document = as_yaml(contract("id", "id"))

        async def deprecate(number: int) -> None:
            response = await stack.client.post(
                f"{BASE}/rule-sets/{set_id}/versions/{number}/deprecate", headers=auth(admin)
            )
            assert response.status_code == 200, response.text

        async def run_with(version: int | str) -> dict[str, Any]:
            payload = run_payload(
                document, attachments=[{"rule_set_id": set_id, "version": version}]
            )
            response = await post_run(stack, chain.team, ed, payload)
            assert response.status_code == 200, response.text
            body: dict[str, Any] = response.json()
            return body

        await deprecate(2)
        latest = await run_with("latest")
        assert [
            (p["version"], p["deprecated_version"])
            for p in latest["pins"]
            if p["name"] == "Two versions"
        ] == [(1, False)]
        exact = await run_with(2)
        assert [
            (p["version"], p["deprecated_version"])
            for p in exact["pins"]
            if p["name"] == "Two versions"
        ] == [(2, True)]

        await deprecate(1)
        every = await run_with("latest")
        assert [
            (p["version"], p["deprecated_version"])
            for p in every["pins"]
            if p["name"] == "Two versions"
        ] == [(2, True)]


async def test_attaching_an_already_mandatory_set_resolves_it_once_as_mandatory(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, ed, _ = await setup(stack)
        set_id = await mandatory_enforced(stack, chain.org, pam)

        body = (
            await post_run(
                stack,
                chain.team,
                ed,
                run_payload(
                    as_yaml(contract("id", "id")),
                    attachments=[{"rule_set_id": set_id, "version": "latest"}],
                ),
            )
        ).json()

        assert [p["source"] for p in body["pins"] if p["name"] == "Org naming"] == ["mandatory"]
        assert [f["source"] for f in findings_of(body, "Org naming")] == ["mandatory"]


async def test_schema_violations_make_the_run_unpublishable_without_a_rule_failure(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, _, ed, _ = await setup(stack)
        document = as_yaml(contract("id", "name", version=123))  # `version` must be a string

        for mode in ("publish", "save"):
            body = (
                await post_run(
                    stack,
                    chain.team,
                    ed,
                    run_payload(document, mode=mode, with_ids=mode == "publish"),
                )
            ).json()
            assert body["report"]["structural"]["valid"] is False
            assert "type" in structural_codes(body)
            assert body["report"]["summary"] == {"publishable": False, "blocking_count": 0}


async def test_a_rule_can_read_the_folder_kind_of_the_run(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, pam, ed, _ = await setup(stack)
        kind_rule = custom_rule(
            'ctx.folder_kind == "team"',
            {"schema": [{"name": "t"}]},
            {"schema": [{"name": "t"}]},
            "Only team folders may hold contracts.",
            target="$",
            pass_ctx={"folder_kind": "team"},
            fail_ctx={"folder_kind": "project"},
        )
        await mandatory_enforced(stack, chain.org, pam, rules={"team-only": kind_rule})
        project_editor, project_editor_id = stack.token("pe")
        stack.grant_sql(project_editor_id, "project_editor", chain.project)
        document = as_yaml(contract("id", "name"))

        at_team = (await post_run(stack, chain.team, ed, run_payload(document))).json()
        at_project = (
            await post_run(stack, chain.project, project_editor, run_payload(document))
        ).json()

        assert findings_of(at_team, "Org naming") == []
        assert at_team["report"]["summary"]["publishable"] is True
        failed = findings_of(at_project, "Org naming")
        assert [(f["rule_key"], f["outcome"], f["blocking"]) for f in failed] == [
            ("team-only", "fail", True)
        ]


async def test_no_document_value_reaches_a_response_a_stored_run_or_a_problem(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, ed, _ = await setup(stack)
        await mandatory_enforced(stack, chain.org, pam, rules={"owner-set": OWNER_RULE})
        planted = contract("id", "name", version={"deep": SECRET})  # wrong-typed field
        planted["schema"][0]["note"] = SECRET  # a node on which the enforced rule errors
        # An unknown key name is named in the structural message (quick 261119-wk8: keys only,
        # never values), so it carries its own marker; no document VALUE may appear anywhere.
        planted["schema"][0][SECRET_KEY_NAME] = SECRET

        response = await post_run(stack, chain.team, ed, run_payload(as_yaml(planted)))

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["report"]["structural"]["valid"] is False
        assert any(f["error_code"] == "no_such_key" for f in body["report"]["findings"])
        assert SECRET not in response.text
        named = [
            e["message"]
            for e in body["report"]["structural"]["errors"]
            if e["keyword"] in ("additionalProperties", "unevaluatedProperties")
        ]
        assert any(SECRET_KEY_NAME in message for message in named)
        assert SECRET_KEY_NAME not in str(body["report"]["findings"])
        stored = stack.validation_sql(
            "SELECT report::text AS report, resolved::text AS resolved FROM validation_run "
            "WHERE id = %s",
            body["run_id"],
        )
        assert len(stored) == 1
        assert SECRET not in stored[0]["report"] + stored[0]["resolved"]

        rejected = await post_run(
            stack, chain.team, ed, run_payload(as_yaml(contract("id", apiVersion=SECRET_VERSION)))
        )
        assert rejected.status_code == 422
        assert rejected.json()["type"] == "urn:dc:problem:unsupported-odcs-version"
        assert SECRET_VERSION not in rejected.text
        assert (
            rejected.json()["detail"]
            == "The document's apiVersion is not a supported ODCS version."
        )
        assert rejected.json()["errors"][0]["pointer"] == "/apiVersion"


async def test_oversized_malformed_and_incomplete_requests_are_refused_without_a_run(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, _, ed, _ = await setup(stack)

        # one byte over the route's own cap (two base64 documents, envelope and ref_facts slack)
        cap = run_body_cap(stack.app.state.settings)
        prefix, suffix = b'{"filler": "', b'"}'
        huge = prefix + b"x" * (cap + 1 - len(prefix) - len(suffix)) + suffix
        assert len(huge) == cap + 1
        too_big = await post_run(stack, chain.team, ed, huge)
        assert too_big.status_code == 413
        assert too_big.json()["type"] == "urn:dc:problem:payload-too-large"

        big_document = as_yaml(contract("id", note="x" * 1_100_000))
        document_too_big = await post_run(stack, chain.team, ed, run_payload(big_document))
        assert document_too_big.status_code == 413

        malformed = await post_run(stack, chain.team, ed, run_payload(b"a: [unclosed"))
        assert malformed.status_code == 422
        assert malformed.json()["type"] == "urn:dc:problem:malformed-document"

        no_draft = run_payload(as_yaml(contract("id")))
        del no_draft["draft_id"]
        missing = await post_run(stack, chain.team, ed, no_draft)
        assert missing.status_code == 422
        assert missing.json()["type"] == "urn:dc:problem:validation-error"
        assert missing.json()["errors"][0]["pointer"] == "/draft_id"

        bad_base64 = run_payload(as_yaml(contract("id")))
        bad_base64["document_b64"] = "***not base64***"
        assert (await post_run(stack, chain.team, ed, bad_base64)).status_code == 422
        assert (await post_run(stack, chain.team, ed, b"{not json")).status_code == 422
        assert len(runs(stack)) == 0


async def test_more_attachments_than_the_cap_are_refused_before_any_resolution(
    make_stack: Any,
) -> None:
    async with make_stack(max_attachments=1) as stack:
        chain, _, ed, _ = await setup(stack)
        attachments = [{"rule_set_id": str(uuid.uuid4()), "version": "latest"} for _ in range(2)]

        refused = await post_run(
            stack, chain.team, ed, run_payload(as_yaml(contract("id")), attachments=attachments)
        )

        assert refused.status_code == 422, refused.text
        assert refused.json()["type"] == "urn:dc:problem:validation-error"
        assert refused.json()["errors"] == [
            {"pointer": "/attachments", "detail": "Too many attachments."}
        ]
        assert len(runs(stack)) == 0


async def test_a_saturated_sandbox_answers_503_with_retry_after(make_stack: Any) -> None:
    async with make_stack(max_concurrent_runs=1) as stack:
        chain, pam, ed, _ = await setup(stack)
        await mandatory_enforced(
            stack, chain.org, pam
        )  # a rule must exist for the sandbox to be used
        async with stack.app.state.sandbox.slot():
            response = await post_run(
                stack, chain.team, ed, run_payload(as_yaml(contract("id", "name")))
            )
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"
        assert response.json()["type"] == "urn:dc:problem:validation-unavailable"
        assert len(runs(stack)) == 0
