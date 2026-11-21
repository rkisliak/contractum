"""Internal folder usage and purge routes (quick 261119-0v1): counts, refusals, trigger scope."""

from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

BASE = "/api/v1/validation"
UNIQUE_NAMES = "this.properties.map(p, p.name).distinct().size() == this.properties.size()"


def table(*names: str) -> dict[str, Any]:
    return {"schema": [{"name": "orders", "properties": [{"name": n} for n in names]}]}


def rule_body() -> str:
    return json.dumps(
        {
            "target": "$.schema[*]",
            "condition": UNIQUE_NAMES,
            "severity": "error",
            "message": "Names must be unique",
            "fixtures": {
                "pass": [{"document": table("id", "name")}],
                "fail": [{"document": table("id", "id")}],
            },
        }
    )


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def usage_url(folder: str) -> str:
    return f"/internal/v1/validation/folders/{folder}/usage"


def purge_url(folder: str) -> str:
    return f"/internal/v1/validation/folders/{folder}/purge"


async def new_set(stack: Any, folder: str, token: str, name: str, *, publish: bool = True) -> str:
    created = await stack.create_rule_set(folder, name, token=token)
    assert created.status_code == 201, created.text
    rule_set_id: str = created.json()["id"]
    if publish:
        current = await stack.get(f"{BASE}/rule-sets/{rule_set_id}", token=token)
        put = await stack.put_rule(
            rule_set_id, "names-unique", rule_body(), current.headers["ETag"], token=token
        )
        assert put.status_code == 200, put.text
        published = await stack.publish(rule_set_id, put.headers["ETag"], token=token)
        assert published.status_code == 201, published.text
    return rule_set_id


async def mandate(stack: Any, rule_set_id: str, token: str, value: bool) -> None:
    resp = await stack.client.put(
        f"{BASE}/rule-sets/{rule_set_id}/mandatory", json={"mandatory": value}, headers=auth(token)
    )
    assert resp.status_code == 200, resp.text


async def test_usage_counts_mandatory_sets_as_blockers_and_the_rest_as_purges(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        pm, principal = stack.token("pm")
        stack.grant_sql(principal, "policy_manager", chain.team)
        empty = await stack.client.get(usage_url(chain.team), headers=auth(stack.admin_token))
        assert empty.status_code == 200, empty.text
        # The seeded platform sets exist but are not the folder's.
        assert empty.json() == {"folder_id": chain.team, "rule_sets": 0, "unused_rule_sets": 0}
        await new_set(stack, chain.team, pm, "With versions")
        await new_set(stack, chain.team, pm, "Draft only", publish=False)
        mandatory = await new_set(stack, chain.team, pm, "Mandatory one")
        await mandate(stack, mandatory, pm, True)
        got = await stack.client.get(usage_url(chain.team), headers=auth(stack.admin_token))
        assert got.json() == {"folder_id": chain.team, "rule_sets": 1, "unused_rule_sets": 2}


async def test_the_routes_need_folder_delete(make_stack: Any) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        editor, principal = stack.token("editor")
        stack.grant_sql(principal, "team_editor", chain.team)
        stranger, _ = stack.token("stranger")
        for method, url in (("GET", usage_url(chain.team)), ("POST", purge_url(chain.team))):
            assert (await stack.client.request(method, url)).status_code == 401
            denied = await stack.client.request(method, url, headers=auth(editor))
            assert denied.status_code == 403
            hidden = await stack.client.request(method, url, headers=auth(stranger))
            assert hidden.status_code == 404


async def test_purge_refuses_a_mandatory_set_then_removes_the_rest_and_audits_counts(
    make_stack: Any,
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        other = await stack.chain()
        pm, principal = stack.token("pm")
        stack.grant_sql(principal, "policy_manager", chain.team)
        stack.grant_sql(principal, "policy_manager", other.team)
        mandatory = await new_set(stack, chain.team, pm, "Mandatory one")
        await mandate(stack, mandatory, pm, True)
        await new_set(stack, chain.team, pm, "Plain")
        await new_set(stack, other.team, pm, "Elsewhere")

        refused = await stack.client.post(purge_url(chain.team), headers=auth(stack.admin_token))
        assert refused.status_code == 409, refused.text
        assert refused.json()["blockers"] == [{"kind": "rule_sets", "count": 1}]
        assert (
            stack.validation_sql(
                "SELECT count(*) AS n FROM rule_set WHERE folder_id = %s", chain.team
            )[0]["n"]
            == 2
        )

        await mandate(stack, mandatory, pm, False)
        purged = await stack.client.post(purge_url(chain.team), headers=auth(stack.admin_token))
        assert purged.status_code == 200, purged.text
        assert purged.json() == {"folder_id": chain.team, "rule_sets": 2, "rule_set_versions": 2}
        assert (
            stack.validation_sql(
                "SELECT count(*) AS n FROM rule_set WHERE folder_id = %s", chain.team
            )[0]["n"]
            == 0
        )
        # Another folder's sets and the platform sets are untouched.
        assert (
            stack.validation_sql(
                "SELECT count(*) AS n FROM rule_set WHERE folder_id = %s", other.team
            )[0]["n"]
            == 1
        )
        assert (
            stack.validation_sql("SELECT count(*) AS n FROM rule_set WHERE scope = 'platform'")[0][
                "n"
            ]
            >= 1
        )
        events = stack.validation_sql(
            "SELECT after FROM outbox WHERE action = 'folder.purged' AND aggregate_id = %s",
            chain.team,
        )
        assert [e["after"] for e in events] == [{"rule_sets": 2, "rule_set_versions": 2}]

        # Idempotent: a second purge finds nothing and says so.
        again = await stack.client.post(purge_url(chain.team), headers=auth(stack.admin_token))
        assert again.json()["rule_sets"] == 0


async def test_the_bypass_never_reaches_mandatory_or_platform_versions(make_stack: Any) -> None:
    """With the purge setting on, versions of a mandatory or platform set still cannot go."""
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        pm, principal = stack.token("pm")
        stack.grant_sql(principal, "policy_manager", chain.team)
        mandatory = await new_set(stack, chain.team, pm, "Mandatory one")
        await mandate(stack, mandatory, pm, True)
        plain = await new_set(stack, chain.team, pm, "Plain")
        url = stack.validation_db_url.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(url, autocommit=True) as conn:
            # No setting: even an unused set's version stays.
            with pytest.raises(psycopg.errors.RaiseException):
                conn.execute("DELETE FROM rule_set_version WHERE rule_set_id = %s", (plain,))
            for where, params in (
                ("rule_set_id = %s", (mandatory,)),
                ("rule_set_id IN (SELECT id FROM rule_set WHERE scope = 'platform')", ()),
            ):
                with conn.transaction():
                    conn.execute("SELECT set_config('dc.folder_purge', 'on', true)")
                    with pytest.raises(psycopg.errors.RaiseException):
                        conn.execute(f"DELETE FROM rule_set_version WHERE {where}", params)  # type: ignore[arg-type]  # noqa: S608
