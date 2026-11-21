"""Rule-set attachments through the real validation app (plan 05-18; RULE-06, VAL-07, D-122..D-131).

identity, validation and contracts run in one process on three databases. A contract owner attaches
a published rule set (a global one included); an attached error rule blocks publish exactly like an
enforced mandatory one, an attached warning never does, and a change of the attachment set between
the validation run and the publish lock answers 409 ``rules-changed`` with nothing published.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

CONTRACTS = "/api/v1/contracts/contracts"

NO_TMP_NAMES = '!this.properties.exists(p, p.name.startsWith("tmp_"))'


def doc(odcs_id: str, *names: str, version: str = "1.0.0") -> bytes:
    """A conformant ODCS v3.2.0 document with one table holding the named properties."""
    props = "".join(f"      - name: {n}\n        logicalType: string\n" for n in names)
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        f"status: draft\nname: Orders\nschema:\n  - name: orders\n    properties:\n{props}"
    ).encode()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def new_id() -> str:
    return f"urn:dc:attach:{uuid.uuid4().hex}"


def table(*names: str) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": n} for n in names]}]}


def no_tmp_rule(*, severity: str = "error", message: str = "no tmp_") -> str:
    """A rule that fails when any property name starts with ``tmp_``."""
    return json.dumps(
        {
            "target": "$.schema[*]",
            "condition": NO_TMP_NAMES,
            "severity": severity,
            "message": message,
            "fixtures": {
                "pass": [{"document": table("id", "name")}],
                "fail": [{"document": table("tmp_id", "name")}],
            },
        }
    )


async def editor(stack: Any, team: str) -> str:
    token, principal_id = stack.token(f"ed-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal_id, "team_editor", team)
    return str(token)


async def save(stack: Any, contract: str, draft: str, etag: str, body: bytes, token: str) -> str:
    saved = await stack.client.put(
        f"{CONTRACTS}/{contract}/drafts/{draft}",
        content=body,
        headers={**bearer(token), "Content-Type": "application/yaml", "If-Match": etag},
    )
    assert saved.status_code == 200, saved.text
    return str(saved.headers["etag"])


async def attach(
    stack: Any, contract: str, rule_set: str, token: str, body: dict[str, Any] | None = None
) -> httpx.Response:
    return await stack.client.put(
        f"{CONTRACTS}/{contract}/rule-sets/{rule_set}",
        json={} if body is None else body,
        headers=bearer(token),
    )


async def detach(stack: Any, contract: str, rule_set: str, token: str) -> httpx.Response:
    return await stack.client.delete(
        f"{CONTRACTS}/{contract}/rule-sets/{rule_set}", headers=bearer(token)
    )


async def listing(stack: Any, contract: str, token: str) -> httpx.Response:
    return await stack.client.get(f"{CONTRACTS}/{contract}/rule-sets", headers=bearer(token))


def outbox_count(stack: Any, sql: Sql, action: str) -> int:
    rows = sql(stack.contracts_db_url, "SELECT count(*) AS n FROM outbox WHERE action = %s", action)
    return int(rows[0]["n"])


def pins_of(stack: Any, sql: Sql, draft: str) -> list[tuple[str, str, str]]:
    row = sql(
        stack.contracts_db_url, "SELECT pinned_rule_sets FROM contract_version WHERE id = %s", draft
    )[0]
    return [(p["name"], p["source"], p["mode"]) for p in row["pinned_rule_sets"]]


async def root_rule_set(
    stack: Any, name: str = "Global extras", rules: dict[str, str] | None = None
) -> tuple[str, str]:
    """A Global Policy Manager publishes a (not mandatory) rule set at ROOT; (id, pm token)."""
    pm = stack.policy_manager(stack.root_id)
    rule_set_id = await stack.new_rule_set(
        stack.root_id, pm, rules or {"no-tmp-names": no_tmp_rule()}, name=name
    )
    return rule_set_id, pm


# -- tracer ---------------------------------------------------------------------------------------


async def test_a_team_editor_attaches_a_root_rule_set_is_blocked_by_it_then_publishes_after_detach(
    make_stack: MakeStack, sql: Sql, invariant: Any
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        global_set, _pm = await root_rule_set(stack)
        ed = await editor(stack, chain.team)
        odcs_id = new_id()
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(odcs_id, "tmp_id", "name"), token=ed
        )

        attached = await attach(stack, contract, global_set, ed)

        assert attached.status_code == 201, attached.text
        body = attached.json()
        assert body["status"] == "created"
        assert body["attachments_revision"] == 1
        assert body["attachment"]["rule_set_id"] == global_set
        assert body["attachment"]["rule_set_name"] == "Global extras"
        assert body["attachment"]["version"] == "latest"
        assert attached.headers["location"] == f"{CONTRACTS}/{contract}/rule-sets/{global_set}"
        assert outbox_count(stack, sql, "contract.ruleset_attached") == 1

        listed = await listing(stack, contract, ed)
        assert listed.status_code == 200, listed.text
        assert [i["rule_set_id"] for i in listed.json()["items"]] == [global_set]
        assert listed.json()["attachments_revision"] == 1

        refused = await stack.publish(contract, draft, etag, token=ed)

        assert refused.status_code == 422, refused.text
        problem = refused.json()
        assert problem["type"] == "urn:dc:problem:contract-validation-failed"
        found = [f for f in problem["report"]["findings"] if f["rule_key"] == "no-tmp-names"]
        assert len(found) == 1
        assert found[0]["source"] == "explicit"
        assert found[0]["mode"] == "enforce"
        assert found[0]["rule_set"]["name"] == "Global extras"
        assert found[0]["blocking"] is True

        removed = await detach(stack, contract, global_set, ed)
        assert removed.status_code == 204, removed.text
        assert (await listing(stack, contract, ed)).json() == {
            "items": [],
            "attachments_revision": 2,
        }
        assert outbox_count(stack, sql, "contract.ruleset_detached") == 1

        published = await stack.publish(contract, draft, etag, token=ed)
        assert published.status_code == 201, published.text

        # A fresh contract without the offending name attaches again and publishes with the pin.
        contract2, draft2, etag2 = await stack.create_contract(
            chain.team, doc(new_id(), "id", "name"), token=ed
        )
        assert (await attach(stack, contract2, global_set, ed)).status_code == 201
        published2 = await stack.publish(contract2, draft2, etag2, token=ed)
        assert published2.status_code == 201, published2.text
        assert ("Global extras", "explicit", "enforce") in pins_of(stack, sql, draft2)
        assert invariant(stack.contracts_db_url) == []


# -- adjacency, empty and ordering edges (RULE-06) ------------------------------------------------


async def test_attaching_the_same_rule_set_again_is_a_noop_and_a_new_version_updates_the_row(
    make_stack: MakeStack, sql: Sql, invariant: Any
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        rule_set, pm = await root_rule_set(stack)
        await stack.publish_rule_version(rule_set, pm, {"no-tmp-names": no_tmp_rule(message="v2")})
        ed = await editor(stack, chain.team)
        contract, _draft, _etag = await stack.create_contract(
            chain.team, doc(new_id(), "id"), token=ed
        )

        first = await attach(stack, contract, rule_set, ed)
        again = await attach(stack, contract, rule_set, ed)

        assert first.status_code == 201, first.text
        assert again.status_code == 200, again.text
        assert again.json()["status"] == "unchanged"
        assert again.json()["attachments_revision"] == 1
        assert outbox_count(stack, sql, "contract.ruleset_attached") == 1

        pinned = await attach(stack, contract, rule_set, ed, {"version": 1})

        assert pinned.status_code == 200, pinned.text
        assert pinned.json()["status"] == "updated"
        assert pinned.json()["attachments_revision"] == 2
        assert pinned.json()["attachment"]["version"] == 1
        assert outbox_count(stack, sql, "contract.ruleset_attached") == 2
        rows = sql(stack.contracts_db_url, "SELECT version_spec FROM contract_ruleset_attachment")
        assert [r["version_spec"] for r in rows] == ["1"]
        assert invariant(stack.contracts_db_url) == []


async def test_a_contract_without_attachments_lists_empty_and_detaching_nothing_is_404(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        rule_set, _pm = await root_rule_set(stack)
        ed = await editor(stack, chain.team)
        contract, _draft, _etag = await stack.create_contract(
            chain.team, doc(new_id(), "id"), token=ed
        )

        listed = await listing(stack, contract, ed)
        removed = await detach(stack, contract, rule_set, ed)

        assert listed.status_code == 200, listed.text
        assert listed.json() == {"items": [], "attachments_revision": 0}
        assert removed.status_code == 404, removed.text
        assert removed.json()["type"] == "urn:dc:problem:not-found"
        assert outbox_count(stack, sql, "contract.ruleset_detached") == 0


async def test_attachments_are_listed_by_lower_cased_name_then_id(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        pm = stack.policy_manager(stack.root_id)
        ids: dict[str, str] = {}
        for name in ("charlie", "Bravo", "alpha"):
            ids[name] = await stack.new_rule_set(
                stack.root_id, pm, {"no-tmp-names": no_tmp_rule()}, name=name
            )
        ed = await editor(stack, chain.team)
        contract, _draft, _etag = await stack.create_contract(
            chain.team, doc(new_id(), "id"), token=ed
        )
        for name in ("charlie", "Bravo", "alpha"):
            assert (await attach(stack, contract, ids[name], ed)).status_code == 201

        listed = await listing(stack, contract, ed)

        assert [i["rule_set_name"] for i in listed.json()["items"]] == ["alpha", "Bravo", "charlie"]
        assert listed.json()["attachments_revision"] == 3


# -- what can be attached -------------------------------------------------------------------------


async def test_a_sibling_unknown_or_never_published_rule_set_is_422_rule_set_not_attachable(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        sibling_team = await stack.team_in_org(chain.org, "other")
        sibling_pm = stack.policy_manager(sibling_team)
        sibling_set = await stack.new_rule_set(
            sibling_team, sibling_pm, {"no-tmp-names": no_tmp_rule()}, name="Sibling only"
        )
        root_pm = stack.policy_manager(stack.root_id)
        draft_only = await stack.validation.post(
            f"/api/v1/validation/folders/{stack.root_id}/rule-sets",
            json={"name": "Never published", "description": "d"},
            headers=bearer(root_pm),
        )
        assert draft_only.status_code == 201, draft_only.text
        ed = await editor(stack, chain.team)
        contract, _draft, _etag = await stack.create_contract(
            chain.team, doc(new_id(), "id"), token=ed
        )

        bodies = []
        for rule_set in (sibling_set, str(uuid.uuid4()), str(draft_only.json()["id"])):
            refused = await attach(stack, contract, rule_set, ed)
            assert refused.status_code == 422, refused.text
            problem = refused.json()
            assert problem["type"] == "urn:dc:problem:rule-set-not-attachable"
            assert rule_set not in problem["detail"]
            bodies.append(problem["detail"])

        assert len(set(bodies)) == 1
        assert (await listing(stack, contract, ed)).json()["attachments_revision"] == 0
        assert outbox_count(stack, sql, "contract.ruleset_attached") == 0


async def test_a_deprecated_version_cannot_be_attached_but_a_later_one_can(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        only, pm = await root_rule_set(stack, name="Only deprecated")
        two, _ = await root_rule_set(stack, name="Two versions")
        await stack.publish_rule_version(two, pm, {"no-tmp-names": no_tmp_rule(message="v2")})
        for rule_set, version in ((only, 1), (two, 1)):
            deprecated = await stack.validation.post(
                f"/api/v1/validation/rule-sets/{rule_set}/versions/{version}/deprecate",
                headers=bearer(stack.admin_token),
            )
            assert deprecated.status_code == 200, deprecated.text
        ed = await editor(stack, chain.team)
        contract, _draft, _etag = await stack.create_contract(
            chain.team, doc(new_id(), "id"), token=ed
        )

        latest_of_only = await attach(stack, contract, only, ed)
        deprecated_exact = await attach(stack, contract, two, ed, {"version": 1})
        later_exact = await attach(stack, contract, two, ed, {"version": 2})
        unknown_exact = await attach(stack, contract, two, ed, {"version": 9})

        assert latest_of_only.status_code == 422, latest_of_only.text
        assert deprecated_exact.status_code == 422, deprecated_exact.text
        assert later_exact.status_code == 201, later_exact.text
        assert later_exact.json()["attachment"]["version"] == 2
        assert unknown_exact.status_code == 422, unknown_exact.text
        for refused in (latest_of_only, deprecated_exact, unknown_exact):
            assert refused.json()["type"] == "urn:dc:problem:rule-set-not-attachable"


async def test_attaching_a_mandatory_rule_set_is_a_200_noop_that_says_so(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        mandatory = await stack.mandatory_rule_set(chain.org, name="Org mandatory")
        ed = await editor(stack, chain.team)
        contract, _draft, _etag = await stack.create_contract(
            chain.team, doc(new_id(), "id"), token=ed
        )

        reported = await attach(stack, contract, mandatory, ed)

        assert reported.status_code == 200, reported.text
        assert reported.json() == {
            "attachment": None,
            "status": "mandatory",
            "attachments_revision": 0,
        }
        assert (await listing(stack, contract, ed)).json() == {
            "items": [],
            "attachments_revision": 0,
        }
        assert sql(stack.contracts_db_url, "SELECT 1 FROM contract_ruleset_attachment") == []
        assert outbox_count(stack, sql, "contract.ruleset_attached") == 0
        # It still applies as mandatory: there is nothing to detach.
        assert (await detach(stack, contract, mandatory, ed)).status_code == 404


# -- who may, and when ----------------------------------------------------------------------------


async def test_only_contract_owners_attach_and_detach_and_a_policy_manager_can_list(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        rule_set, _pm = await root_rule_set(stack)
        ed = await editor(stack, chain.team)
        contract, _draft, _etag = await stack.create_contract(
            chain.team, doc(new_id(), "id"), token=ed
        )
        assert (await attach(stack, contract, rule_set, ed)).status_code == 201
        reader, reader_id = stack.token(f"rd-{uuid.uuid4().hex[:6]}")
        stack.grant_sql(reader_id, "team_reader", chain.team)
        stranger, _ = stack.token(f"st-{uuid.uuid4().hex[:6]}")

        for token, status in ((reader, 403), (stranger, 404)):
            put = await attach(stack, contract, rule_set, token)
            delete = await detach(stack, contract, rule_set, token)
            assert put.status_code == status, put.text
            assert delete.status_code == status, delete.text
        anonymous = await stack.client.put(f"{CONTRACTS}/{contract}/rule-sets/{rule_set}", json={})
        assert anonymous.status_code == 401
        # Nothing changed, and a Policy Manager at the Organization reads the list.
        manager = stack.policy_manager(chain.org)
        listed = await listing(stack, contract, manager)
        assert listed.status_code == 200, listed.text
        assert [i["rule_set_id"] for i in listed.json()["items"]] == [rule_set]
        assert (await listing(stack, contract, stranger)).status_code == 404
        assert outbox_count(stack, sql, "contract.ruleset_detached") == 0


async def test_an_archived_folder_refuses_attach_and_detach(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        rule_set, _pm = await root_rule_set(stack)
        ed = await editor(stack, chain.team)
        contract, _draft, _etag = await stack.create_contract(
            chain.team, doc(new_id(), "id"), token=ed
        )
        assert (await attach(stack, contract, rule_set, ed)).status_code == 201
        archived = await stack.identity.post(
            f"/api/v1/identity/folders/{chain.project}/archive", headers=bearer(stack.admin_token)
        )
        assert archived.status_code == 200, archived.text

        put = await attach(stack, contract, rule_set, ed, {"version": 1})
        delete = await detach(stack, contract, rule_set, ed)

        for refused in (put, delete):
            assert refused.status_code == 409, refused.text
            assert refused.json()["type"] == "urn:dc:problem:folder-archived"
        assert outbox_count(stack, sql, "contract.ruleset_attached") == 1
        assert outbox_count(stack, sql, "contract.ruleset_detached") == 0


async def test_with_validation_down_an_attach_is_503_and_nothing_is_attached(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        rule_set, _pm = await root_rule_set(stack)
        ed = await editor(stack, chain.team)
        contract, _draft, _etag = await stack.create_contract(
            chain.team, doc(new_id(), "id"), token=ed
        )

        stack.validation_link.down = True
        down = await attach(stack, contract, rule_set, ed)
        stack.validation_link.restore()
        stack.validation_link.force(500, body=b"boom")
        broken = await attach(stack, contract, rule_set, ed)
        stack.validation_link.restore()

        for refused in (down, broken):
            assert refused.status_code == 503, refused.text
            assert refused.json()["type"] == "urn:dc:problem:validation-unavailable"
            assert "boom" not in refused.text
        assert (await listing(stack, contract, ed)).json() == {
            "items": [],
            "attachments_revision": 0,
        }
        assert sql(stack.contracts_db_url, "SELECT 1 FROM contract_ruleset_attachment") == []
        assert (await attach(stack, contract, rule_set, ed)).status_code == 201


async def test_attaching_to_an_unknown_contract_is_404(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        rule_set, _pm = await root_rule_set(stack)
        ed = await editor(stack, chain.team)

        refused = await attach(stack, str(uuid.uuid4()), rule_set, ed)

        assert refused.status_code == 404, refused.text


# -- publish with attachments ---------------------------------------------------------------------


class RacingGate:
    """Wraps the installed gate: after it answers, runs ``act`` once, before publish takes locks."""

    def __init__(self, inner: Any, act: Callable[[], Any]) -> None:
        self._inner = inner
        self._act = act
        self.fired = False

    async def check(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._inner.check(*args, **kwargs)
        if not self.fired:
            self.fired = True
            await self._act()
        return result


async def test_a_change_to_the_attachments_between_validation_and_publish_is_409_rules_changed(
    make_stack: MakeStack, sql: Sql, invariant: Any
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        first_set, _pm = await root_rule_set(stack, name="First set")
        second_set, _pm2 = await root_rule_set(stack, name="Second set")
        ed = await editor(stack, chain.team)
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(new_id(), "id", "name"), token=ed
        )
        assert (await attach(stack, contract, first_set, ed)).status_code == 201
        published_before = outbox_count(stack, sql, "contract.version_published")

        async def attach_second() -> None:
            assert (await attach(stack, contract, second_set, ed)).status_code == 201

        real_gate = stack.contracts_app.state.publish_gate
        stack.contracts_app.state.publish_gate = RacingGate(real_gate, attach_second)
        try:
            raced = await stack.publish(contract, draft, etag, token=ed)
            assert raced.status_code == 409, raced.text
            assert raced.json()["type"] == "urn:dc:problem:rules-changed"
            row = sql(
                stack.contracts_db_url,
                "SELECT state, validation_run_id FROM contract_version WHERE id = %s",
                draft,
            )[0]
            assert row == {"state": "draft", "validation_run_id": None}
            assert outbox_count(stack, sql, "contract.version_published") == published_before

            # The retry validates the new attachment set and publishes.
            retried = await stack.publish(contract, draft, etag, token=ed)
        finally:
            stack.contracts_app.state.publish_gate = real_gate
        assert retried.status_code == 201, retried.text
        names = {name for name, _source, _mode in pins_of(stack, sql, draft)}
        assert {"First set", "Second set"} <= names
        assert invariant(stack.contracts_db_url) == []


async def test_an_attached_warning_rule_never_blocks_a_publish_and_is_reported_not_blocking(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        rule_set, _pm = await root_rule_set(
            stack, name="Soft extras", rules={"no-tmp-names": no_tmp_rule(severity="warning")}
        )
        ed = await editor(stack, chain.team)
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(new_id(), "tmp_id", "name"), token=ed
        )
        assert (await attach(stack, contract, rule_set, ed)).status_code == 201

        published = await stack.publish(contract, draft, etag, token=ed)

        assert published.status_code == 201, published.text
        runs = sql(stack.validation_db_url, "SELECT report, publishable FROM validation_run")
        assert [r["publishable"] for r in runs] == [True]
        findings = [f for f in runs[0]["report"]["findings"] if f["rule_key"] == "no-tmp-names"]
        assert len(findings) == 1
        assert findings[0]["severity"] == "warning"
        assert findings[0]["blocking"] is False
        assert findings[0]["source"] == "explicit"
        assert ("Soft extras", "explicit", "enforce") in pins_of(stack, sql, draft)


async def test_an_exact_version_attachment_pins_that_version_not_the_latest(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        rule_set, pm = await root_rule_set(stack)
        # Version 2 would block "tmp_id"; version 1 pinned never sees it as an error rule at 2.
        await stack.publish_rule_version(rule_set, pm, {"no-tmp-names": no_tmp_rule(message="v2")})
        ed = await editor(stack, chain.team)
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(new_id(), "id", "name"), token=ed
        )
        assert (await attach(stack, contract, rule_set, ed, {"version": 1})).status_code == 201

        published = await stack.publish(contract, draft, etag, token=ed)

        assert published.status_code == 201, published.text
        row = sql(
            stack.contracts_db_url,
            "SELECT pinned_rule_sets FROM contract_version WHERE id = %s",
            draft,
        )[0]
        pinned = [p for p in row["pinned_rule_sets"] if p["name"] == "Global extras"]
        assert [(p["version"], p["source"]) for p in pinned] == [(1, "explicit")]


# -- WR-04: no pooled connection sits idle in transaction across the visibility call -------------


async def test_no_contracts_connection_is_idle_in_transaction_during_an_attach(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        global_set, _pm = await root_rule_set(stack)
        ed = await editor(stack, chain.team)
        contract, _draft, _etag = await stack.create_contract(
            chain.team, doc(new_id(), "id", "name"), token=ed
        )
        seen: list[int] = []

        async def probe(request: Any) -> None:
            seen.append(
                int(
                    sql(
                        stack.contracts_db_url,
                        "SELECT count(*) AS n FROM pg_stat_activity "
                        "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                        "AND state LIKE 'idle in transaction%%'",
                    )[0]["n"]
                )
            )

        stack.validation_link.on_request = probe
        attached = await attach(stack, contract, global_set, ed)

        assert attached.status_code == 201, attached.text
        assert seen and set(seen) == {0}


# -- WR-06: attachments are capped at attach time, not discovered at publish ----------------------


async def test_attaching_past_the_cap_is_422_and_a_re_pin_at_the_cap_still_works(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(max_attachments=2) as stack:
        chain = await stack.chain()
        first, _ = await root_rule_set(stack, "Extras one")
        second, _ = await root_rule_set(stack, "Extras two")
        third, _ = await root_rule_set(stack, "Extras three")
        ed = await editor(stack, chain.team)
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(new_id(), "id", "name"), token=ed
        )

        assert (await attach(stack, contract, first, ed)).status_code == 201
        assert (await attach(stack, contract, second, ed)).status_code == 201
        refused = await attach(stack, contract, third, ed)

        assert refused.status_code == 422, refused.text
        assert refused.json()["type"] == "urn:dc:problem:validation-error"
        # Nothing was written and a re-pin of an attached set at the cap is not a new attachment.
        assert len((await listing(stack, contract, ed)).json()["items"]) == 2
        repin = await attach(stack, contract, second, ed, {"version": 1})
        assert repin.status_code == 200, repin.text
        # The contract still publishes: it never crossed validation's limit.
        published = await stack.publish(contract, draft, etag, token=ed)
        assert published.status_code == 201, published.text
