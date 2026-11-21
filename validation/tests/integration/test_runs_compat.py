"""Compatibility in the internal validation run (plan 06-06; VAL-02, VAL-03, D-147, D-148, D-152).

A run may carry a baseline (the previously published version) and the stable major floor. The
report then holds the compatibility section and, for a breaking change without a major bump, the
native blocking finding ``semver-breaking-change``. The Plan 07 stack runs identity and validation
in one process; the helpers are small copies of ``test_runs.py``.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

BASE = "/api/v1/validation"
YAML = "application/yaml"


def table(*names: str) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": n} for n in names]}]}


def contract(*names: str, version: str = "1.0.0") -> dict[str, Any]:
    return {
        "kind": "DataContract",
        "apiVersion": "v3.2.0",
        "id": "urn:datacontract:test:orders",
        "version": version,
        "status": "draft",
        "schema": [
            {
                "name": "orders",
                "properties": [{"name": n, "logicalType": "string"} for n in names],
            }
        ],
    }


def as_yaml(document: dict[str, Any]) -> bytes:
    return yaml.safe_dump(document, sort_keys=False).encode()


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def run_url(folder_id: str) -> str:
    return f"/internal/v1/validation/folders/{folder_id}/runs"


def payload(
    document: dict[str, Any],
    *,
    baseline: bytes | dict[str, Any] | None = None,
    floor: int | None = None,
    mode: str = "publish",
    baseline_semver: str = "1.0.0",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "mode": mode,
        "media_type": YAML,
        "document_b64": b64(as_yaml(document)),
        "attachments": [],
        "attachments_revision": 0,
        "stable_major_floor": floor,
    }
    if mode == "publish":
        body["contract_id"] = str(uuid.uuid4())
        body["draft_id"] = str(uuid.uuid4())
    if baseline is not None:
        raw = baseline if isinstance(baseline, bytes) else as_yaml(baseline)
        body["baseline"] = {
            "version_id": str(uuid.uuid4()),
            "semver": baseline_semver,
            "media_type": YAML,
            "document_b64": b64(raw),
        }
    return body


async def setup(stack: Any) -> tuple[Any, str, str]:
    chain = await stack.chain()
    pam, pam_id = stack.token("pam")
    stack.grant_sql(pam_id, "policy_manager", chain.org)
    ted, ted_id = stack.token("ed")
    stack.grant_sql(ted_id, "team_editor", chain.team)
    return chain, pam, ted


async def post(stack: Any, folder: str, token: str, body: dict[str, Any]) -> Any:
    return await stack.client.post(run_url(folder), json=body, headers=auth(token))


def stored(stack: Any) -> list[dict[str, Any]]:
    return stack.validation_sql("SELECT * FROM validation_run ORDER BY id")


def native(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in body["report"]["findings"] if f["source"] == "native"]


# -- tracer ---------------------------------------------------------------------------------------


async def test_a_publish_run_removing_a_property_under_a_minor_bump_needs_a_major_version(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, _pam, ed = await setup(stack)
        before = contract("id", "amount")

        response = await post(
            stack,
            chain.team,
            ed,
            payload(contract("id", version="1.1.0"), baseline=before, floor=1),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        report = body["report"]
        assert report["summary"]["publishable"] is False
        (finding,) = native(body)
        assert finding["rule_key"] == "semver-breaking-change"
        assert finding["severity"] == "error"
        assert finding["blocking"] is True
        assert finding["rule_set"]["id"] is None
        assert "2.0.0" in finding["message"]
        assert report["summary"]["blocking_count"] == 1
        compat = report["compatibility"]
        assert compat["status"] == "compared"
        assert compat["required_bump"] == "major"
        assert compat["bump_ok"] is False
        assert compat["counts"] == {"breaking": 1, "warning": 0, "info": 0}
        assert compat["classifier_version"]
        assert compat["declared_version"] == "1.1.0"
        assert compat["baseline"]["semver"] == "1.0.0"
        assert compat["changes"][0]["code"] == "property-removed"
        assert compat["changes"][0]["class"] == "breaking"
        assert compat["changes"][0]["path"] == "schema.orders.properties.amount"
        (run,) = stored(stack)
        assert run["publishable"] is False
        assert run["report"]["compatibility"]["required_bump"] == "major"
        assert run["report"]["compatibility"]["changes"][0]["code"] == "property-removed"


async def test_a_major_bump_passes_and_the_breaking_change_is_still_listed(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, _pam, ed = await setup(stack)

        response = await post(
            stack,
            chain.team,
            ed,
            payload(contract("id", version="2.0.0"), baseline=contract("id", "amount"), floor=1),
        )

        body = response.json()
        assert body["report"]["summary"]["publishable"] is True
        assert native(body) == []
        compat = body["report"]["compatibility"]
        assert compat["bump_ok"] is True
        assert compat["counts"]["breaking"] == 1
        assert compat["changes"][0]["code"] == "property-removed"


async def test_without_a_stable_major_floor_a_breaking_change_is_listed_and_publishable(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, _pam, ed = await setup(stack)

        response = await post(
            stack,
            chain.team,
            ed,
            payload(
                contract("id", version="0.2.0"),
                baseline=contract("id", "amount", version="0.1.0"),
                floor=None,
            ),
        )

        body = response.json()
        assert body["report"]["summary"]["publishable"] is True
        assert body["report"]["compatibility"]["counts"]["breaking"] == 1


async def test_a_run_without_a_baseline_says_no_baseline_and_adds_no_finding(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, _pam, ed = await setup(stack)

        response = await post(stack, chain.team, ed, payload(contract("id"), floor=1))

        body = response.json()
        assert body["report"]["compatibility"]["status"] == "no-baseline"
        assert body["report"]["compatibility"]["baseline"] is None
        assert native(body) == []
        assert body["report"]["summary"]["publishable"] is True


async def test_a_save_run_shows_what_publish_will_do_and_stores_nothing(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, _pam, ed = await setup(stack)

        response = await post(
            stack,
            chain.team,
            ed,
            payload(
                contract("id", version="1.1.0"),
                baseline=contract("id", "amount"),
                floor=1,
                mode="save",
            ),
        )

        body = response.json()
        assert body["run_id"] is None
        (finding,) = native(body)
        assert finding["blocking"] is True
        assert body["report"]["compatibility"]["required_bump"] == "major"
        assert stored(stack) == []


# -- cel-2 bindings -------------------------------------------------------------------------------


def cel2_rule(condition: str, fixtures: dict[str, Any]) -> str:
    return json.dumps(
        {
            "target": "$",
            "condition": condition,
            "severity": "error",
            "message": "Removing a property is not allowed here.",
            "fixtures": fixtures,
        }
    )


async def mandatory_set(
    stack: Any, folder: str, token: str, name: str, rules: dict[str, str]
) -> None:
    created = await stack.create_rule_set(folder, name, token=token)
    assert created.status_code == 201, created.text
    set_id = created.json()["id"]
    etag = (await stack.get(f"{BASE}/rule-sets/{set_id}", token=token)).headers["ETag"]
    for key, body in rules.items():
        put = await stack.put_rule(set_id, key, body, etag, token=token)
        assert put.status_code == 200, put.text
        etag = put.headers["ETag"]
    published = await stack.publish(set_id, etag, token=token)
    assert published.status_code == 201, published.text
    for method, url in (
        ("put", f"{BASE}/rule-sets/{set_id}/mandatory"),
        ("post", f"{BASE}/rule-sets/{set_id}/versions/1/enforce-now"),
    ):
        kwargs = {"json": {"mandatory": True}} if method == "put" else {}
        response = await getattr(stack.client, method)(url, headers=auth(token), **kwargs)
        assert response.status_code == 200, response.text


async def test_cel2_rules_see_the_baseline_as_previous_and_the_classified_changes(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        chain, pam, ed = await setup(stack)
        await mandatory_set(
            stack,
            chain.org,
            pam,
            "Compatibility",
            {
                "guarded-previous": cel2_rule(
                    "previous == null || has(previous.schema)",
                    {
                        "pass": [{"document": table("id")}],
                        "fail": [{"document": table("id"), "previous": {"other": 1}}],
                    },
                ),
                "no-removals": cel2_rule(
                    "!changes.exists(c, c['code'] == 'property-removed')",
                    {
                        "pass": [{"document": table("id")}],
                        "fail": [{"document": table("id"), "previous": table("id", "name")}],
                    },
                ),
            },
        )

        removal = await post(
            stack,
            chain.team,
            ed,
            payload(contract("id", version="2.0.0"), baseline=contract("id", "amount"), floor=1),
        )
        keep = await post(
            stack,
            chain.team,
            ed,
            payload(contract("id", "amount", version="1.0.1"), baseline=contract("id", "amount")),
        )
        first = await post(stack, chain.team, ed, payload(contract("id")))

        failed = {
            f["rule_key"]: f
            for f in removal.json()["report"]["findings"]
            if f["rule_set"]["name"] == "Compatibility"
        }
        assert set(failed) == {"no-removals"}
        assert failed["no-removals"]["outcome"] == "fail"
        assert failed["no-removals"]["blocking"] is True
        for response in (keep, first):
            assert [
                f for f in response.json()["report"]["findings"] if f["source"] != "native"
            ] == [], response.text


# -- fail closed (plan 06-06 task 2) -------------------------------------------------


@pytest.mark.parametrize(
    "bad_baseline",
    [b"a: [unclosed", b"a: " + b"x" * 1_100_000, as_yaml({"apiVersion": "v9.9.9", "kind": "x"})],
    ids=["unparseable", "over the size cap", "unsupported apiVersion"],
)
async def test_an_unusable_baseline_fails_a_publish_run_closed_and_only_advises_a_save_run(
    make_stack: Any, bad_baseline: bytes
) -> None:
    async with make_stack() as stack:
        chain, _pam, ed = await setup(stack)
        document = contract("id", version="1.1.0")

        publish = await post(
            stack, chain.team, ed, payload(document, baseline=bad_baseline, floor=1)
        )
        save = await post(
            stack, chain.team, ed, payload(document, baseline=bad_baseline, floor=1, mode="save")
        )

        assert publish.status_code == 200, publish.text
        body = publish.json()
        assert body["report"]["compatibility"]["status"] == "unavailable"
        assert body["report"]["compatibility"]["changes"] == []
        (finding,) = native(body)
        assert finding["rule_key"] == "compatibility-unavailable"
        assert finding["blocking"] is True
        assert body["report"]["summary"]["publishable"] is False
        (run,) = stored(stack)
        assert run["report"]["compatibility"]["status"] == "unavailable"
        assert save.status_code == 200, save.text
        assert save.json()["report"]["compatibility"]["status"] == "unavailable"
        assert native(save.json()) == []
        assert save.json()["report"]["summary"]["publishable"] is True
        assert "unclosed" not in publish.text and "x" * 50 not in publish.text


async def test_a_malformed_baseline_envelope_is_a_422_without_echo(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain, _pam, ed = await setup(stack)
        body = payload(contract("id"), baseline=contract("id"))
        body["baseline"]["document_b64"] = "***not base64***"
        bad_semver = payload(
            contract("id"), baseline=contract("id"), baseline_semver="not a version"
        )
        unknown = payload(contract("id"), baseline=contract("id"))
        unknown["baseline"]["unexpected"] = 1
        negative_floor = payload(contract("id"), floor=-1)

        for request in (body, bad_semver, unknown, negative_floor):
            response = await post(stack, chain.team, ed, request)
            assert response.status_code == 422, response.text
            assert response.json()["type"] == "urn:dc:problem:validation-error"
        assert "not a version" not in (await post(stack, chain.team, ed, bad_semver)).text
        assert stored(stack) == []
