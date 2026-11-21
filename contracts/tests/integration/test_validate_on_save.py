"""Saving a draft returns the advisory validation report (plan 05-19; VAL-06, D-134).

identity, validation and contracts run in one process on three databases. Every route that creates
or saves a draft embeds the save-mode report from validation next to the local structural report.
The embed never blocks the save: when validation is down, slow or busy the save still succeeds and
``validation`` is ``{"status": "unavailable"}``. No save-mode run is stored.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

CONTRACTS = "/api/v1/contracts/contracts"


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
    return f"urn:dc:onsave:{uuid.uuid4().hex}"


async def editor(stack: Any, team: str) -> str:
    token, principal_id = stack.token(f"ed-{uuid.uuid4().hex[:6]}")
    stack.grant_sql(principal_id, "team_editor", team)
    return str(token)


async def put_draft(
    stack: Any, contract: str, draft: str, etag: str, body: bytes, token: str
) -> Any:
    return await stack.client.put(
        f"{CONTRACTS}/{contract}/drafts/{draft}",
        content=body,
        headers={**bearer(token), "Content-Type": "application/yaml", "If-Match": etag},
    )


def run_count(stack: Any, sql: Sql) -> int:
    return int(sql(stack.validation_db_url, "SELECT count(*) AS n FROM validation_run")[0]["n"])


# -- tracer ---------------------------------------------------------------------------------------


async def test_saving_a_draft_returns_the_report_publish_would_apply_without_blocking_the_save(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        await stack.mandatory_rule_set(chain.org)
        ed = await editor(stack, chain.team)
        odcs_id = new_id()
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(odcs_id, "id", "name"), token=ed
        )
        runs_before = run_count(stack, sql)

        saved = await put_draft(stack, contract, draft, etag, doc(odcs_id, "id", "id"), ed)

        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["draft"]["revision"] == 2
        assert body["structural_validation"]["valid"] is True
        validation = body["validation"]
        assert validation["status"] == "completed"
        assert validation["summary"]["publishable"] is False
        blocking = [f for f in validation["findings"] if f["blocking"]]
        assert [(f["rule_key"], f["location"]["pointer"]) for f in blocking] == [
            ("unique-property-names", "/schema/0")
        ]
        # The save is real, and a save stores no validation run.
        row = sql(
            stack.contracts_db_url, "SELECT revision FROM contract_version WHERE id = %s", draft
        )
        assert row[0]["revision"] == 2
        assert run_count(stack, sql) == runs_before


async def test_a_mandatory_rule_in_its_warning_window_is_reported_without_blocking(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        await stack.mandatory_rule_set(chain.org, enforce=False)
        ed = await editor(stack, chain.team)
        odcs_id = new_id()
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(odcs_id, "id", "name"), token=ed
        )

        saved = await put_draft(stack, contract, draft, etag, doc(odcs_id, "id", "id"), ed)

        assert saved.status_code == 200, saved.text
        validation = saved.json()["validation"]
        assert validation["summary"]["publishable"] is True
        found = [f for f in validation["findings"] if f["rule_key"] == "unique-property-names"]
        assert len(found) == 1
        assert found[0]["mode"] == "warn"
        assert found[0]["blocking"] is False


# -- all six routes -------------------------------------------------------------------------------

FIXTURES = Path(__file__).resolve().parents[3] / "shared" / "dc-odcs" / "tests" / "fixtures"

ROUTES = ["upload", "template", "starter", "from-version", "new-draft", "save-draft"]


async def create_via(
    stack: Any, route: str, team: str, org: str, token: str, *, down: bool = False
) -> tuple[Any, str]:
    """Call one draft-producing route; returns (response, contract id).

    ``down`` cuts the validation link right before the route under test (setup needs it up).
    """

    def cut() -> None:
        stack.validation_link.down = down

    folder = f"/api/v1/contracts/folders/{team}/contracts"
    if route == "upload":
        cut()
        response = await stack.post(team, doc(new_id(), "id", "name"), token=token)
    elif route == "template":
        created = await stack.client.post(
            f"/api/v1/contracts/folders/{org}/templates",
            params={"name": "Orders template"},
            content=(FIXTURES / "valid-v3.2.0.yaml").read_bytes(),
            headers={**bearer(token), "Content-Type": "application/yaml"},
        )
        assert created.status_code == 201, created.text
        template_id = created.json()["template"]["id"]
        published = await stack.client.post(
            f"/api/v1/contracts/templates/{template_id}/publish", headers=bearer(token)
        )
        assert published.status_code == 201, published.text
        cut()
        response = await stack.client.post(
            f"{folder}/from-template",
            json={"template_id": template_id, "odcs_id": new_id()},
            headers=bearer(token),
        )
    elif route == "starter":
        cut()
        response = await stack.client.post(
            f"{folder}/starter",
            json={"odcs_id": new_id(), "name": "Starter"},
            headers=bearer(token),
        )
    else:
        source, draft, etag = await stack.create_contract(team, doc(new_id(), "id"), token=token)
        published = await stack.publish(source, draft, etag, token=token)
        assert published.status_code == 201, published.text
        if route == "from-version":
            cut()
            response = await stack.client.post(
                f"{folder}/from-version",
                json={"source_contract_id": source, "source_version": "1.0.0", "odcs_id": new_id()},
                headers=bearer(token),
            )
        elif route == "new-draft":
            cut()
            response = await stack.client.post(
                f"{CONTRACTS}/{source}/drafts",
                json={"from_version": "1.0.0"},
                headers=bearer(token),
            )
            assert response.status_code == 201, response.text
            return response, source
        else:
            new = await stack.client.post(
                f"{CONTRACTS}/{source}/drafts",
                json={"from_version": "1.0.0"},
                headers=bearer(token),
            )
            assert new.status_code == 201, new.text
            new_draft = new.json()["draft"]
            odcs = json.loads(
                (
                    await stack.client.get(
                        f"{CONTRACTS}/{source}/drafts/{new_draft['id']}/document",
                        headers={**bearer(token), "Accept": "application/json"},
                    )
                ).text
            )["id"]
            cut()
            response = await put_draft(
                stack, source, new_draft["id"], new_draft["etag"], doc(odcs, "id", "name"), token
            )
            assert response.status_code == 200, response.text
            return response, source
    assert response.status_code == 201, response.text
    return response, str(response.json()["contract"]["id"])


@pytest.mark.parametrize("route", ROUTES)
async def test_every_draft_producing_route_embeds_a_completed_report(
    make_stack: MakeStack, route: str
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()

        response, _contract = await create_via(
            stack, route, chain.team, chain.org, stack.admin_token
        )

        body = response.json()
        assert body["validation"]["status"] == "completed"
        assert isinstance(body["validation"]["findings"], list)
        assert "publishable" in body["validation"]["summary"]
        assert "structural_validation" in body


@pytest.mark.parametrize("route", ROUTES)
async def test_with_validation_down_every_route_still_saves_and_reports_unavailable(
    make_stack: MakeStack, sql: Sql, route: str
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()

        response, contract = await create_via(
            stack, route, chain.team, chain.org, stack.admin_token, down=True
        )

        body = response.json()
        assert body["validation"] == {"status": "unavailable"}
        assert "structural_validation" in body
        rows = sql(
            stack.contracts_db_url, "SELECT count(*) AS n FROM contract WHERE id = %s", contract
        )
        assert rows[0]["n"] == 1


async def test_a_slow_validation_is_cut_at_the_save_timeout_and_the_save_still_succeeds(
    make_stack: MakeStack,
) -> None:
    async with make_stack(validation_save_timeout_s=0.3) as stack:
        chain = await stack.chain()
        stack.validation_link.delay_s = 3.0
        started = time.monotonic()

        response = await stack.post(chain.team, doc(new_id(), "id"))

        assert response.status_code == 201, response.text
        assert response.json()["validation"] == {"status": "unavailable"}
        assert time.monotonic() - started < 2.0


async def test_a_busy_validation_is_reported_unavailable_and_the_save_succeeds(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        stack.validation_link.force(503, {"Retry-After": "1"})

        response = await stack.post(chain.team, doc(new_id(), "id"))

        assert response.status_code == 201, response.text
        assert response.json()["validation"] == {"status": "unavailable"}


async def test_the_embed_forwards_the_callers_bearer_and_the_contracts_attachments(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        ed = await editor(stack, chain.team)
        odcs_id = new_id()
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(odcs_id, "id"), token=ed
        )
        stack.validation_link.requests.clear()

        saved = await put_draft(stack, contract, draft, etag, doc(odcs_id, "id", "name"), ed)

        assert saved.status_code == 200, saved.text
        calls = [r for r in stack.validation_link.requests if r.url.path.endswith("/runs")]
        assert len(calls) == 1
        assert calls[0].headers["authorization"] == f"Bearer {ed}"
        sent = json.loads(calls[0].content)
        assert sent["mode"] == "save"
        assert sent["attachments"] == []
        assert base64.b64decode(sent["document_b64"]).startswith(b"apiVersion: v3.2.0")


# -- SEC-1: an identical save holds no draft lock and no open transaction during the embed -------


async def test_an_identical_save_releases_the_draft_lock_before_the_validation_call(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        ed = await editor(stack, chain.team)
        body = doc(new_id(), "id", "name")
        contract, draft, etag = await stack.create_contract(chain.team, body, token=ed)
        probes: list[tuple[int, bool]] = []

        async def probe(request: Any) -> None:
            idle = int(
                sql(
                    stack.contracts_db_url,
                    "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = "
                    "current_database() AND pid <> pg_backend_pid() "
                    "AND state LIKE 'idle in transaction%%'",
                )[0]["n"]
            )
            try:  # NOWAIT raises at once when another transaction holds the row lock
                sql(
                    stack.contracts_db_url,
                    "SELECT id FROM contract_version WHERE id = %s FOR UPDATE NOWAIT",
                    draft,
                )
                free = True
            except Exception:
                free = False
            probes.append((idle, free))

        stack.validation_link.on_request = probe
        saved = await put_draft(stack, contract, draft, etag, body, ed)  # identical bytes

        assert saved.status_code == 200, saved.text
        assert saved.json()["draft"]["revision"] == 1  # nothing changed
        assert saved.headers["etag"] == etag
        assert saved.json()["validation"]["status"] == "completed"
        assert probes == [(0, True)]  # one validation call; no lock and no open transaction
