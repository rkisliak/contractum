"""Rule-set dry-runs through contracts and the real validation app (RULE-10, D-158, D-159).

``POST /api/v1/contracts/governance/rule-set-dry-runs`` takes ``{rule_set_id, version, folder_id?,
cursor?}``. Contracts learns the rule set's folder from validation with the caller's token, checks
``ruleset:dry-run`` there, lists the next keyset page of in-scope contracts (Latest versions only)
and sends their documents to validation's internal dry-run route. Results carry metadata, outcomes,
rule keys and messages only (D-36); JSON Pointers only for contracts the caller may read with
``contract:read`` (CR-01). Nothing changes state (D-161), but every answered call writes one
counts-only audit event (``governance.rule_set_dry_run``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

DRY_RUNS = "/api/v1/contracts/governance/rule-set-dry-runs"
DRY_RUN_PATH = "/dry-run"
# A property named "legacy" is forbidden (the platform seed already refuses duplicate names).
NO_LEGACY = 'this.properties.all(p, p.name != "legacy")'


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, *names: str, version: str = "1.0.0", description: str = "") -> bytes:
    """A conformant ODCS v3.2.0 document with one table holding the named properties."""
    props = "".join(
        f"      - name: {n}\n        logicalType: string\n"
        + (f"        description: {description}\n" if description else "")
        for n in names
    )
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        f"status: active\nname: Orders\nschema:\n  - name: orders\n    properties:\n{props}"
    ).encode()


def rule(message: str = "No legacy property.", condition: str = NO_LEGACY) -> str:
    table = lambda *n: {"schema": [{"name": "orders", "properties": [{"name": x} for x in n]}]}  # noqa: E731
    return json.dumps(
        {
            "target": "$.schema[*]",
            "condition": condition,
            "severity": "error",
            "message": message,
            "fixtures": {
                "pass": [{"document": table("id", "name")}],
                "fail": [{"document": table("id", "legacy")}],
            },
        }
    )


async def draft_rule_set(stack: Any, folder: str, token: str, rules: dict[str, str]) -> str:
    """A rule set at ``folder`` whose draft holds ``rules`` (never published)."""
    created = await stack.validation.post(
        f"/api/v1/validation/folders/{folder}/rule-sets",
        json={"name": f"impact-{uuid.uuid4().hex[:6]}", "description": "rules"},
        headers=bearer(token),
    )
    assert created.status_code == 201, created.text
    rule_set_id = str(created.json()["id"])
    base = f"/api/v1/validation/rule-sets/{rule_set_id}"
    etag = (await stack.validation.get(base, headers=bearer(token))).headers["ETag"]
    for key, body in rules.items():
        put = await stack.validation.put(
            f"{base}/draft/rules/{key}",
            content=body,
            headers={**bearer(token), "If-Match": etag, "Content-Type": "application/json"},
        )
        assert put.status_code == 200, put.text
        etag = put.headers["ETag"]
    return rule_set_id


async def published(stack: Any, team: str, *names: str, odcs_id: str | None = None) -> str:
    """A contract in ``team`` whose Latest (1.0.0) holds the named properties."""
    contract, draft, etag = await stack.create_contract(
        team, doc(odcs_id or f"urn:dc:impact:{uuid.uuid4().hex}", *names)
    )
    response = await stack.publish(contract, draft, etag)
    assert response.status_code == 201, response.text
    return str(contract)


async def world(stack: Any) -> dict[str, str]:
    """Organization O with teams T1 (A fails the draft rule) and T2 (B passes); a PM at O."""
    org = await stack.folder(stack.root_id, f"o{uuid.uuid4().hex[:10]}")
    t1 = await stack.team_in_org(org, "one")
    t2 = await stack.team_in_org(org, "two")
    pm = stack.policy_manager(org)
    rule_set = await draft_rule_set(stack, org, pm, {"no-legacy": rule()})
    a = await published(stack, t1, "id", "legacy")
    b = await published(stack, t2, "id", "name")
    return {"org": org, "t1": t1, "t2": t2, "pm": pm, "rule_set": rule_set, "a": a, "b": b}


def dry_run_calls(stack: Any) -> list[Any]:
    return [r for r in stack.validation_link.requests if r.url.path.endswith(DRY_RUN_PATH)]


def outbox_count(sql: Sql, stack: Any) -> int:
    return int(sql(stack.contracts_db_url, "SELECT count(*) AS n FROM outbox")[0]["n"])


def audit_rows(sql: Sql, stack: Any, action: str) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT aggregate_type, aggregate_id, actor, scope_path, before, after FROM outbox"
        " WHERE action = %s ORDER BY id",
        action,
    )


async def test_a_policy_manager_dry_runs_a_draft_and_sees_which_team_contract_would_fail(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        before = outbox_count(sql, stack)

        response = await stack.client.post(
            DRY_RUNS,
            json={"rule_set_id": w["rule_set"], "version": "draft"},
            headers=bearer(w["pm"]),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["rule_set_id"] == w["rule_set"]
        assert body["version"] == "draft"
        assert body["scope_folder_id"] == w["org"]
        assert body["total_in_scope"] == 2
        assert body["next_cursor"] is None
        assert body["summary"] == {"would_fail": 1, "would_error": 0, "pass": 1, "hidden": 0}
        by_id = {item["contract_id"]: item for item in body["items"]}
        assert set(by_id) == {w["a"], w["b"]}
        a, b = by_id[w["a"]], by_id[w["b"]]
        assert a["would_block"] is True and b["would_block"] is False
        assert a["folder_id"] == w["t1"] and b["folder_id"] == w["t2"]
        assert a["latest_semver"] == "1.0.0" and a["name"] == "Orders"
        assert a["counts"] == {"fail": 1, "error": 0, "warn": 0}
        [failing] = a["failing_rules"]
        assert failing["rule_key"] == "no-legacy"
        assert failing["message"] == "No legacy property."
        # CR-01: a Policy Manager holds no contract:read, so where the rule matched is withheld.
        assert failing["pointers"] == [] and failing["pointers_total"] == 0
        assert failing["pointers_withheld"] is True
        assert b["failing_rules"] == []
        # Only metadata, outcomes, rule keys, messages and pointers: never document fields.
        assert set(a) == {
            "contract_id",
            "name",
            "odcs_id",
            "folder_id",
            "latest_semver",
            "status",
            "would_block",
            "counts",
            "failing_rules",
        }
        # The caller's own bearer went to validation; the only write is one audit event.
        [call] = dry_run_calls(stack)
        assert call.headers["authorization"] == f"Bearer {w['pm']}"
        assert outbox_count(sql, stack) == before + 1


async def test_every_dry_run_is_audited_with_counts_only(make_stack: MakeStack, sql: Sql) -> None:
    async with make_stack() as stack:
        w = await world(stack)

        response = await stack.client.post(
            DRY_RUNS,
            json={"rule_set_id": w["rule_set"], "version": "draft"},
            headers=bearer(w["pm"]),
        )

        assert response.status_code == 200, response.text
        [row] = audit_rows(sql, stack, "governance.rule_set_dry_run")
        assert (row["aggregate_type"], row["aggregate_id"]) == ("rule_set", w["rule_set"])
        assert row["before"] is None
        assert row["after"] == {
            "version": "draft",
            "scope_folder_id": w["org"],
            "continued": False,
            "listed": 2,
            "total_in_scope": 2,
            "would_fail": 1,
            "would_error": 0,
            "pass": 1,
            "hidden": 0,
        }
        assert [str(f) for f in row["scope_path"]][-1] == w["org"]
        assert row["actor"]["type"] == "user"
        # No contract id, name, rule key or pointer reaches the audit trail.
        text = json.dumps(row, default=str)
        for leaked in (w["a"], w["b"], "no-legacy", "Orders", "/schema/0"):
            assert leaked not in text


async def test_pointers_are_returned_only_where_the_caller_holds_contract_read(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        sub = f"pm-reader-{uuid.uuid4().hex[:6]}"
        token = stack.policy_manager(w["org"], sub=sub)
        # The same Policy Manager may read T1's contracts (and A fails there).
        stack.grant_sql(f"fake:{sub}", "team_reader", w["t1"])
        c = await published(stack, w["t2"], "id", "legacy")

        response = await stack.client.post(
            DRY_RUNS, json={"rule_set_id": w["rule_set"], "version": "draft"}, headers=bearer(token)
        )

        assert response.status_code == 200, response.text
        by_id = {item["contract_id"]: item for item in response.json()["items"]}
        [readable] = by_id[w["a"]]["failing_rules"]
        assert readable["pointers"] == ["/schema/0"] and readable["pointers_total"] == 1
        assert readable["pointers_withheld"] is False
        [withheld] = by_id[c]["failing_rules"]
        assert withheld["rule_key"] == "no-legacy" and by_id[c]["would_block"] is True
        assert withheld["pointers"] == [] and withheld["pointers_withheld"] is True


class NoContentScope:
    """The real authorizer, except that the ``contract:read`` scope cannot be answered."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def check(self, *args: Any, **kwargs: Any) -> Any:
        return await self._inner.check(*args, **kwargs)

    async def accessible_folders(self, principal: Any, permission: str) -> Any:
        if permission == "contract:read":
            from dc_auth.authorizer import AuthorizerUnavailable

            raise AuthorizerUnavailable("injected failure")
        return await self._inner.accessible_folders(principal, permission)


async def test_an_outage_of_the_content_scope_is_503_not_pointers(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        state = stack.contracts_app.state
        original = state.authorizer_factory
        state.authorizer_factory = lambda request: NoContentScope(original(request))
        try:
            response = await stack.client.post(
                DRY_RUNS,
                json={"rule_set_id": w["rule_set"], "version": "draft"},
                headers=bearer(w["pm"]),
            )
        finally:
            state.authorizer_factory = original
        assert response.status_code == 503, response.text
        assert response.json()["type"] == "urn:dc:problem:service-unavailable"
        assert "/schema/0" not in response.text
        assert audit_rows(sql, stack, "governance.rule_set_dry_run") == []


async def test_results_never_carry_document_content(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        org = await stack.folder(stack.root_id, f"o{uuid.uuid4().hex[:10]}")
        team = await stack.team_in_org(org, "one")
        pm = stack.policy_manager(org)
        rule_set = await draft_rule_set(stack, org, pm, {"no-legacy": rule()})
        contract, draft, etag = await stack.create_contract(
            team,
            doc("urn:dc:impact:secret", "secretcolumn", "legacy", description="topsecret"),
        )
        assert (await stack.publish(contract, draft, etag)).status_code == 201

        response = await stack.client.post(
            DRY_RUNS, json={"rule_set_id": rule_set, "version": "draft"}, headers=bearer(pm)
        )

        assert response.status_code == 200, response.text
        assert "secretcolumn" not in response.text
        assert "topsecret" not in response.text
        assert response.json()["items"][0]["would_block"] is True


async def test_a_team_editor_an_unknown_and_a_platform_rule_set_get_the_uniform_404(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(platform_seed=True) as stack:
        w = await world(stack)
        editor, principal = stack.token(f"ed-{uuid.uuid4().hex[:6]}")
        stack.grant_sql(principal, "team_editor", w["t1"])
        platform = sql(stack.validation_db_url, "SELECT id FROM rule_set WHERE scope = 'platform'")
        assert platform

        answers = []
        for token, rule_set in (
            (editor, w["rule_set"]),
            (w["pm"], str(uuid.uuid4())),
            (w["pm"], str(platform[0]["id"])),
            (stack.admin_token, str(platform[0]["id"])),
        ):
            response = await stack.client.post(
                DRY_RUNS, json={"rule_set_id": rule_set, "version": "draft"}, headers=bearer(token)
            )
            answers.append(response)

        for response in answers:
            assert response.status_code == 404, response.text
            assert response.json()["type"] == "urn:dc:problem:not-found"
        # Validation's batch route was never called for any of them.
        assert dry_run_calls(stack) == []


async def test_no_bearer_is_401_before_the_body_is_read(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        response = await stack.client.post(
            DRY_RUNS,
            content=b"x" * 10_000,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 401, response.text
        assert stack.validation_link.requests == []


# -- Task 2: paging, caps, hidden names, narrowing and failures ------------------------------


async def dry_run(stack: Any, token: str, **body: Any) -> Any:
    return await stack.client.post(DRY_RUNS, json=body, headers=bearer(token))


async def walk(stack: Any, token: str, **body: Any) -> list[dict[str, Any]]:
    """Every page of a dry-run, following ``next_cursor``; returns the page bodies."""
    pages: list[dict[str, Any]] = []
    cursor = None
    while True:
        response = await dry_run(stack, token, **body, cursor=cursor)
        assert response.status_code == 200, response.text
        pages.append(response.json())
        cursor = pages[-1]["next_cursor"]
        if cursor is None:
            return pages


def sent_items(stack: Any) -> list[int]:
    return [len(json.loads(r.content)["items"]) for r in dry_run_calls(stack)]


async def test_exactly_a_page_is_one_page_and_one_more_is_a_second_page_of_one(
    make_stack: MakeStack,
) -> None:
    async with make_stack(impact_page_size=2) as stack:
        w = await world(stack)
        body = {"rule_set_id": w["rule_set"], "version": "draft"}

        [only] = await walk(stack, w["pm"], **body)
        assert len(only["items"]) == 2 and only["next_cursor"] is None
        assert only["total_in_scope"] == 2

        c = await published(stack, w["t2"], "id", "other")
        pages = await walk(stack, w["pm"], **body)

        assert [len(p["items"]) for p in pages] == [2, 1]
        assert all(p["total_in_scope"] == 3 for p in pages)
        visited = [item["contract_id"] for p in pages for item in p["items"]]
        assert sorted(visited) == sorted({w["a"], w["b"], c})
        assert visited == sorted(visited, key=uuid.UUID), "pages are ordered by contract id"
        assert max(sent_items(stack)) <= 2


async def test_a_scope_without_published_live_contracts_answers_empty_without_validation(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        org = await stack.folder(stack.root_id, f"o{uuid.uuid4().hex[:10]}")
        team = await stack.team_in_org(org, "one")
        pm = stack.policy_manager(org)
        rule_set = await draft_rule_set(stack, org, pm, {"no-legacy": rule()})
        body = {"rule_set_id": rule_set, "version": "draft"}
        empty = {
            "items": [],
            "summary": {"would_fail": 0, "would_error": 0, "pass": 0, "hidden": 0},
            "total_in_scope": 0,
            "next_cursor": None,
        }

        nothing = (await dry_run(stack, pm, **body)).json()
        await stack.create_contract(team, doc(f"urn:dc:impact:{uuid.uuid4().hex}", "id"))
        deleted = await published(stack, team, "id", "legacy")
        sql(
            stack.contracts_db_url,
            "UPDATE contract SET deleted_at = now(), deleted_by = 'system:test' WHERE id = %s",
            deleted,
        )
        draft_and_deleted = (await dry_run(stack, pm, **body)).json()

        for answer in (nothing, draft_and_deleted):
            assert {k: answer[k] for k in empty} == empty
        assert dry_run_calls(stack) == []


class NarrowMetadata:
    """The real authorizer, except that ``contract:read-metadata`` excludes one subtree."""

    def __init__(self, inner: Any, excluded: str) -> None:
        self._inner = inner
        self._excluded = uuid.UUID(excluded)

    async def check(self, *args: Any, **kwargs: Any) -> Any:
        return await self._inner.check(*args, **kwargs)

    async def accessible_folders(self, principal: Any, permission: str) -> Any:
        scope = await self._inner.accessible_folders(principal, permission)
        if permission != "contract:read-metadata":
            return scope
        return scope.model_copy(
            update={
                "excluded_subtrees": frozenset({*scope.excluded_subtrees, self._excluded}),
                "folder_ids": frozenset(scope.folder_ids) - {self._excluded},
            }
        )


async def test_a_contract_the_caller_cannot_read_metadata_of_is_counted_as_hidden(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        state = stack.contracts_app.state
        original = state.authorizer_factory
        state.authorizer_factory = lambda request: NarrowMetadata(original(request), w["t2"])
        try:
            response = await dry_run(stack, w["pm"], rule_set_id=w["rule_set"], version="draft")
        finally:
            state.authorizer_factory = original

        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["contract_id"] for item in body["items"]] == [w["a"]]
        assert body["summary"] == {"would_fail": 1, "would_error": 0, "pass": 1, "hidden": 1}
        assert body["total_in_scope"] == 2
        assert w["b"] not in response.text


async def test_a_blocked_subtree_never_reaches_validation(make_stack: MakeStack, sql: Sql) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        sql(
            stack.identity_db_url,
            "UPDATE folder SET block_inheritance = true WHERE id = %s",
            w["t2"],
        )

        response = await dry_run(stack, w["pm"], rule_set_id=w["rule_set"], version="draft")

        assert response.status_code == 200, response.text
        body = response.json()
        assert [item["contract_id"] for item in body["items"]] == [w["a"]]
        assert body["total_in_scope"] == 1
        assert sent_items(stack) == [1]


async def test_validation_saturation_and_outages_are_503(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        link = stack.validation_link
        body = {"rule_set_id": w["rule_set"], "version": "draft"}

        async def saturate(request: Any) -> None:
            if request.url.path.endswith(DRY_RUN_PATH):
                link.force(503, {"Retry-After": "7"}, b'{"type":"urn:dc:problem:x"}')

        link.on_request = saturate
        busy = await dry_run(stack, w["pm"], **body)
        link.restore()
        assert busy.status_code == 503, busy.text
        assert busy.headers["retry-after"] == "7"
        assert busy.json()["type"] == "urn:dc:problem:validation-unavailable"

        async def cut(request: Any) -> None:
            if request.url.path.endswith(DRY_RUN_PATH):
                link.down = True

        link.on_request = cut
        down = await dry_run(stack, w["pm"], **body)
        link.restore()
        assert down.status_code == 503, down.text
        assert down.json()["type"] == "urn:dc:problem:validation-unavailable"

        # Validation unreachable while resolving the rule set's folder: no batch call at all.
        link.requests.clear()
        link.down = True
        unresolved = await dry_run(stack, w["pm"], **body)
        link.restore()
        assert unresolved.status_code == 503, unresolved.text
        assert unresolved.json()["type"] == "urn:dc:problem:validation-unavailable"
        assert dry_run_calls(stack) == []


async def test_the_byte_budget_cuts_a_page_but_keeps_at_least_one_contract(
    make_stack: MakeStack,
) -> None:
    async with make_stack(impact_page_max_bytes=1) as stack:
        w = await world(stack)

        pages = await walk(stack, w["pm"], rule_set_id=w["rule_set"], version="draft")

        assert [len(p["items"]) for p in pages] == [1, 1]
        assert sent_items(stack) == [1, 1]


async def test_the_folder_budget_cuts_a_page(make_stack: MakeStack) -> None:
    async with make_stack(impact_page_max_folders=1) as stack:
        w = await world(stack)

        pages = await walk(stack, w["pm"], rule_set_id=w["rule_set"], version="draft")

        assert [len(p["items"]) for p in pages] == [1, 1]


async def test_folder_id_narrows_to_a_team_and_outside_the_subtree_is_empty(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        other_org = await stack.folder(stack.root_id, f"o{uuid.uuid4().hex[:10]}")
        body = {"rule_set_id": w["rule_set"], "version": "draft"}

        narrowed = (await dry_run(stack, w["pm"], **body, folder_id=w["t1"])).json()
        assert [item["contract_id"] for item in narrowed["items"]] == [w["a"]]
        assert narrowed["total_in_scope"] == 1
        assert narrowed["scope_folder_id"] == w["t1"]
        calls = len(dry_run_calls(stack))

        for outside in (other_org, stack.root_id):
            answer = await dry_run(stack, w["pm"], **body, folder_id=outside)
            assert answer.status_code == 200, answer.text
            empty = answer.json()
            assert (empty["items"], empty["total_in_scope"], empty["next_cursor"]) == ([], 0, None)
        assert len(dry_run_calls(stack)) == calls


async def test_bodies_too_large_or_invalid_are_refused_before_any_contract_query(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        w = await world(stack)
        pm = w["pm"]
        rs = w["rule_set"]

        large = await stack.client.post(
            DRY_RUNS,
            content=json.dumps({"rule_set_id": rs, "version": "draft", "cursor": "x" * 5000}),
            headers={**bearer(pm), "Content-Type": "application/json"},
        )
        assert large.status_code == 413, large.text
        assert large.json()["type"] == "urn:dc:problem:payload-too-large"

        for bad in (
            {"rule_set_id": rs, "version": "draft", "extra": 1},
            {"rule_set_id": rs, "version": "latest"},
            {"rule_set_id": rs, "version": 0},
            {"rule_set_id": "not-a-uuid", "version": "draft"},
            {"version": "draft"},
        ):
            refused = await dry_run(stack, pm, **bad)
            assert refused.status_code == 422, (bad, refused.text)
            assert refused.json()["type"] == "urn:dc:problem:validation-error"
            assert "latest" not in refused.text and "not-a-uuid" not in refused.text

        bad_cursor = await dry_run(stack, pm, rule_set_id=rs, version="draft", cursor="nope")
        assert bad_cursor.status_code == 422, bad_cursor.text
        assert bad_cursor.json()["errors"][0]["pointer"] == "/body/cursor"
        assert dry_run_calls(stack) == []
