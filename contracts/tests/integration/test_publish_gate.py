"""The publish gate through the real validation app (plan 05-15; VAL-07, RULE-07, D-126..D-132).

identity, validation and contracts run in one process on three databases; contracts reaches
validation over a switchable ``ValidationLink`` exactly as in compose, with the caller's own bearer.
A contract that breaks an enforced mandatory rule is refused with the report; the fixed contract
publishes; the published version records the rule-set versions it was validated against and the
database refuses to change them.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]

CONTRACTS = "/api/v1/contracts/contracts"


def doc(odcs_id: str, *names: str, version: str = "1.0.0", status: str = "active") -> bytes:
    """A conformant ODCS v3.2.0 document with one table holding the named properties."""
    props = "".join(f"      - name: {n}\n        logicalType: string\n" for n in names)
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        f"status: {status}\nname: Orders\nschema:\n  - name: orders\n    properties:\n{props}"
    ).encode()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def new_id() -> str:
    return f"urn:dc:gate:{uuid.uuid4().hex}"


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


def published_events(stack: Any, sql: Sql) -> int:
    rows = sql(
        stack.contracts_db_url,
        "SELECT count(*) AS n FROM outbox WHERE action = 'contract.version_published'",
    )
    return int(rows[0]["n"])


def version_row(stack: Any, sql: Sql, draft: str) -> dict[str, Any]:
    return sql(stack.contracts_db_url, "SELECT * FROM contract_version WHERE id = %s", draft)[0]


# -- tracer ---------------------------------------------------------------------------------------


async def test_an_enforced_mandatory_rule_blocks_a_publish_with_the_report_and_the_fix_publishes(
    make_stack: MakeStack, sql: Sql, invariant: Any
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        rule_set_id = await stack.mandatory_rule_set(chain.org)
        ed = await editor(stack, chain.team)
        odcs_id = new_id()
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(odcs_id, "id", "id"), token=ed
        )
        events_before = published_events(stack, sql)

        refused = await stack.publish(contract, draft, etag, token=ed)

        assert refused.status_code == 422, refused.text
        problem = refused.json()
        assert problem["type"] == "urn:dc:problem:contract-validation-failed"
        assert problem["run_id"]
        report = problem["report"]
        assert report["summary"]["publishable"] is False
        found = [f for f in report["findings"] if f["rule_key"] == "unique-property-names"]
        assert len(found) == 1
        finding = found[0]
        assert finding["rule_set"]["name"] == "Org naming"
        assert finding["rule_set"]["id"] == rule_set_id
        assert finding["rule_set"]["folder_id"] == chain.org
        assert finding["rule_set"]["version"] == 1
        assert finding["location"]["pointer"] == "/schema/0"
        assert finding["blocking"] is True
        # The draft is untouched and nothing was published.
        row = version_row(stack, sql, draft)
        assert row["state"] == "draft"
        assert row["revision"] == 1
        assert published_events(stack, sql) == events_before
        # The run is stored by validation, blocked.
        stored = sql(
            stack.validation_db_url, "SELECT id::text AS id, publishable FROM validation_run"
        )
        assert [(r["id"], r["publishable"]) for r in stored] == [(problem["run_id"], False)]

        fixed_etag = await save(stack, contract, draft, etag, doc(odcs_id, "id", "name"), ed)
        published = await stack.publish(contract, draft, fixed_etag, token=ed)

        assert published.status_code == 201, published.text
        assert version_row(stack, sql, draft)["state"] == "published"
        assert published_events(stack, sql) == events_before + 1
        assert invariant(stack.contracts_db_url) == []


async def test_a_mandatory_rule_at_an_organization_gates_a_contract_created_there(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        org_rule_set = await stack.mandatory_rule_set(chain.org)
        # A set below the contract's folder never applies to it: ancestors only.
        await stack.mandatory_rule_set(chain.project, name="Project naming")
        admin, principal = stack.token("org-admin")
        stack.grant_sql(principal, "org_admin", chain.org)
        odcs_id = new_id()
        contract, draft, etag = await stack.create_contract(
            chain.org, doc(odcs_id, "id", "id"), token=admin
        )

        refused = await stack.publish(contract, draft, etag, token=admin)

        assert refused.status_code == 422, refused.text
        problem = refused.json()
        assert problem["type"] == "urn:dc:problem:contract-validation-failed"
        found = [
            f for f in problem["report"]["findings"] if f["rule_key"] == "unique-property-names"
        ]
        assert [f["rule_set"]["folder_id"] for f in found] == [chain.org]
        assert {f["rule_set"]["name"] for f in problem["report"]["findings"]} <= {
            "Org naming",
            "odcs-structural",
            "odcs-consistency",
            "odcs-publication",
            "odcs-references",
        }

        fixed_etag = await save(stack, contract, draft, etag, doc(odcs_id, "id", "name"), admin)
        published = await stack.publish(contract, draft, fixed_etag, token=admin)

        assert published.status_code == 201, published.text
        pins = pins_of_row(version_row(stack, sql, draft))
        assert [p["name"] for p in pins if p["scope"] == "folder"] == ["Org naming"]
        assert [p["rule_set_id"] for p in pins if p["scope"] == "folder"] == [org_rule_set]


async def test_a_contract_directly_at_root_publishes_with_only_platform_and_root_sets(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        await stack.mandatory_rule_set(chain.org)  # below ROOT: never applies to a ROOT contract
        root_pm, root_principal = stack.token("root-pm")
        stack.grant_sql(root_principal, "policy_manager", stack.root_id)
        root_set = await stack.mandatory_rule_set(stack.root_id, name="Root rules", token=root_pm)
        odcs_id = new_id()
        contract, draft, etag = await stack.create_contract(stack.root_id, doc(odcs_id, "id", "id"))

        refused = await stack.publish(contract, draft, etag)
        assert refused.status_code == 422, refused.text
        folders = {
            f["rule_set"]["folder_id"]
            for f in refused.json()["report"]["findings"]
            if f["rule_key"] == "unique-property-names"
        }
        assert folders == {stack.root_id}

        saved = await save(
            stack, contract, draft, etag, doc(odcs_id, "id", "name"), stack.admin_token
        )
        published = await stack.publish(contract, draft, saved)
        assert published.status_code == 201, published.text
        pins = pins_of_row(version_row(stack, sql, draft))
        assert [p["rule_set_id"] for p in pins if p["scope"] == "folder"] == [root_set]


async def test_the_gate_forwards_the_callers_own_bearer_and_the_raw_bytes(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        ed = await editor(stack, chain.team)
        body = doc(new_id(), "id", "name")
        contract, draft, etag = await stack.create_contract(chain.team, body, token=ed)
        # The create already made a save-mode call (VAL-06); only the publish is under test.
        stack.validation_link.requests.clear()

        published = await stack.publish(contract, draft, etag, token=ed)

        assert published.status_code == 201, published.text
        sent = [r for r in stack.validation_link.requests if r.url.path.endswith("/runs")]
        assert len(sent) == 1
        request = sent[0]
        assert request.url.path == f"/internal/v1/validation/folders/{chain.team}/runs"
        assert request.headers["authorization"] == f"Bearer {ed}"
        payload = json.loads(request.content)
        assert payload["mode"] == "publish"
        assert payload["contract_id"] == contract
        assert payload["draft_id"] == draft
        assert base64.b64decode(payload["document_b64"]) == body


# -- fail closed (VAL-07, T-5-37, T-5-38) --------------------------------------------------------


async def _draft(stack: Any) -> tuple[str, str, str, str, str]:
    chain = await stack.chain()
    ed = await editor(stack, chain.team)
    contract, draft, etag = await stack.create_contract(
        chain.team, doc(new_id(), "id", "name"), token=ed
    )
    return contract, draft, etag, ed, chain.team


async def _assert_unpublished(stack: Any, sql: Sql, draft: str, events_before: int) -> None:
    row = version_row(stack, sql, draft)
    assert row["state"] == "draft"
    assert published_events(stack, sql) == events_before


async def test_an_unreachable_validation_service_refuses_the_publish_with_503(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        contract, draft, etag, ed, _ = await _draft(stack)
        before = published_events(stack, sql)
        stack.validation_link.down = True

        refused = await stack.publish(contract, draft, etag, token=ed)

        assert refused.status_code == 503, refused.text
        assert refused.json()["type"] == "urn:dc:problem:validation-unavailable"
        await _assert_unpublished(stack, sql, draft, before)
        stack.validation_link.restore()
        assert (await stack.publish(contract, draft, etag, token=ed)).status_code == 201


async def test_a_validation_5xx_is_503_and_copies_retry_after(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        contract, draft, etag, ed, _ = await _draft(stack)
        before = published_events(stack, sql)
        stack.validation_link.force(503, {"Retry-After": "1"})

        refused = await stack.publish(contract, draft, etag, token=ed)

        assert refused.status_code == 503, refused.text
        assert refused.headers["retry-after"] == "1"
        assert refused.json()["type"] == "urn:dc:problem:validation-unavailable"
        stack.validation_link.force(500)
        again = await stack.publish(contract, draft, etag, token=ed)
        assert again.status_code == 503, again.text
        await _assert_unpublished(stack, sql, draft, before)


async def test_a_content_hash_that_differs_from_the_gated_bytes_is_503(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        contract, draft, etag, ed, _ = await _draft(stack)
        before = published_events(stack, sql)
        answer = {
            "run_id": str(uuid.uuid4()),
            "mode": "publish",
            "content_sha256": "0" * 64,
            "engine_version": "x",
            "dsl_version": "cel-1",
            "schema_sha": "y",
            "attachments_revision": None,
            "pins": [],
            "report": {"summary": {"publishable": True, "blocking_count": 0}, "findings": []},
        }
        stack.validation_link.force(
            200, {"Content-Type": "application/json"}, json.dumps(answer).encode()
        )

        refused = await stack.publish(contract, draft, etag, token=ed)

        assert refused.status_code == 503, refused.text
        assert refused.json()["type"] == "urn:dc:problem:validation-unavailable"
        await _assert_unpublished(stack, sql, draft, before)


async def test_a_missing_gate_refuses_every_publish_instead_of_passing_through(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        contract, draft, etag, ed, _ = await _draft(stack)
        before = published_events(stack, sql)
        del stack.contracts_app.state.publish_gate

        refused = await stack.publish(contract, draft, etag, token=ed)

        assert refused.status_code == 503, refused.text
        assert refused.json()["type"] == "urn:dc:problem:validation-unavailable"
        await _assert_unpublished(stack, sql, draft, before)


async def test_a_caller_validation_does_not_authorise_is_refused_403(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        contract, draft, etag, ed, _ = await _draft(stack)
        before = published_events(stack, sql)
        stack.validation_link.force(403)

        refused = await stack.publish(contract, draft, etag, token=ed)

        assert refused.status_code == 403, refused.text
        assert refused.json()["type"] == "urn:dc:problem:forbidden"
        await _assert_unpublished(stack, sql, draft, before)


# -- machine identities pass through the same gate (D-71, D-72, D-126) ----------------------------


async def _local_editor(stack: Any, team: str) -> str:
    """A local user with team_editor on ``team``, signed in interactively; returns its JWT."""
    username = f"pat-{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex + "Aa1!"
    created = await stack.identity.post(
        "/api/v1/identity/users",
        json={"username": username, "password": password, "display_name": username},
        headers=bearer(stack.admin_token),
    )
    assert created.status_code == 201, created.text
    stack.grant_sql(created.json()["principal_id"], "team_editor", team)
    login = await stack.identity.post(
        "/api/v1/identity/auth/login", json={"username": username, "password": password}
    )
    assert login.status_code == 200, login.text
    return str(login.json()["access_token"])


async def _pat(stack: Any, owner_token: str, *, read_only: bool = False) -> str:
    created = await stack.identity.post(
        "/api/v1/identity/pats",
        json={
            "name": f"gate-{uuid.uuid4().hex[:6]}",
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
            "read_only": read_only,
        },
        headers=bearer(owner_token),
    )
    assert created.status_code == 201, created.text
    token = str(created.json()["token"])
    assert token.startswith("dcp_")
    return token


async def test_an_editors_pat_publishes_through_the_same_gate_with_pins(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        ed = await _local_editor(stack, chain.team)
        pat = await _pat(stack, ed)
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(new_id(), "id", "name"), token=pat
        )
        stack.validation_link.requests.clear()  # the create's save-mode call is not under test

        published = await stack.publish(contract, draft, etag, token=pat)

        assert published.status_code == 201, published.text
        sent = [r for r in stack.validation_link.requests if r.url.path.endswith("/runs")]
        assert [r.headers["authorization"] for r in sent] == [f"Bearer {pat}"]
        row = version_row(stack, sql, draft)
        assert row["state"] == "published"
        assert row["validation_run_id"] is not None
        assert [pin["name"] for pin in pins_of_row(row)] == PLATFORM_PIN_NAMES


async def test_a_read_only_pat_is_refused_by_contracts_before_validation_is_called(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        ed = await _local_editor(stack, chain.team)
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(new_id(), "id", "name"), token=ed
        )
        pat = await _pat(stack, ed, read_only=True)
        before = published_events(stack, sql)
        calls = len(stack.validation_link.requests)

        refused = await stack.publish(contract, draft, etag, token=pat)

        assert refused.status_code == 403, refused.text
        assert len(stack.validation_link.requests) == calls
        await _assert_unpublished(stack, sql, draft, before)


# -- the published version records and freezes its pins (RULE-07, D-132, T-5-39) ----------------


def pins_of_row(row: dict[str, Any]) -> list[dict[str, Any]]:
    pins = row["pinned_rule_sets"]
    assert isinstance(pins, list), pins
    return pins


SEMANTIC_PINS = [
    ("odcs-consistency", 1, "mandatory", "warn"),
    ("odcs-publication", 1, "mandatory", "warn"),
    ("odcs-publication", 2, "mandatory", "warn"),
    ("odcs-references", 1, "mandatory", "warn"),
]
PLATFORM_PIN_NAMES = [
    "odcs-consistency",
    "odcs-publication",
    "odcs-publication",
    "odcs-references",
    "odcs-structural",
]


def short(pins: list[dict[str, Any]]) -> list[tuple[str, int, str, str]]:
    """(name, version, source, mode) for each pin, in stored order."""
    return [(p["name"], p["version"], p["source"], p["mode"]) for p in pins]


async def publish_ok(stack: Any, team: str, ed: str, body: bytes | None = None) -> str:
    """Create a contract with a conformant document, publish it; returns the version (draft) id."""
    contract, draft, etag = await stack.create_contract(
        team, body or doc(new_id(), "id", "name"), token=ed
    )
    published = await stack.publish(contract, draft, etag, token=ed)
    assert published.status_code == 201, published.text
    return draft


async def test_a_published_version_records_the_run_and_every_rule_set_version_it_met(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        rule_set_id = await stack.mandatory_rule_set(chain.org)
        ed = await editor(stack, chain.team)
        odcs_id = new_id()
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(odcs_id, "id", "id"), token=ed
        )
        blocked = await stack.publish(contract, draft, etag, token=ed)
        assert blocked.status_code == 422, blocked.text
        fixed_etag = await save(stack, contract, draft, etag, doc(odcs_id, "id", "name"), ed)

        published = await stack.publish(contract, draft, fixed_etag, token=ed)

        assert published.status_code == 201, published.text
        row = version_row(stack, sql, draft)
        runs = sql(
            stack.validation_db_url,
            "SELECT id, publishable FROM validation_run ORDER BY id",
        )
        assert [r["publishable"] for r in runs] == [False, True]
        assert row["validation_run_id"] == runs[-1]["id"]
        assert row["engine_version"]
        assert row["schema_sha"]
        pins = pins_of_row(row)
        assert short(pins) == [
            *SEMANTIC_PINS,
            ("odcs-structural", 1, "mandatory", "enforce"),
            ("Org naming", 1, "mandatory", "enforce"),
        ]
        *_, platform, org = pins
        assert (platform["scope"], platform["folder_id"]) == ("platform", None)
        assert (org["scope"], org["folder_id"], org["rule_set_id"]) == (
            "folder",
            chain.org,
            rule_set_id,
        )
        assert set(org) == {
            "rule_set_id",
            "name",
            "folder_id",
            "scope",
            "version",
            "source",
            "mode",
            "dsl_version",
        }
        event = sql(
            stack.contracts_db_url,
            "SELECT after FROM outbox WHERE action = 'contract.version_published'",
        )[0]["after"]
        assert event["validation_run_id"] == str(row["validation_run_id"])
        assert event["pinned_rule_sets"] == pins


async def test_every_warn_mode_version_of_a_mandatory_set_is_pinned_as_its_own_entry(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        pm = stack.policy_manager(chain.org)
        rule_set_id = await stack.mandatory_rule_set(chain.org, token=pm)
        for number in (2, 3):
            await stack.publish_rule_version(
                rule_set_id, pm, {"unique-property-names": stack.rule_body(f"Names v{number}")}
            )
        ed = await editor(stack, chain.team)

        draft = await publish_ok(stack, chain.team, ed)

        pins = pins_of_row(version_row(stack, sql, draft))
        assert short(pins) == [
            ("Org naming", 1, "mandatory", "enforce"),
            ("Org naming", 2, "mandatory", "warn"),
            ("Org naming", 3, "mandatory", "warn"),
        ]


async def test_a_publish_where_only_the_platform_set_applies_pins_exactly_that_set(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        ed = await editor(stack, chain.team)

        draft = await publish_ok(stack, chain.team, ed)

        pins = pins_of_row(version_row(stack, sql, draft))
        assert short(pins) == [*SEMANTIC_PINS, ("odcs-structural", 1, "mandatory", "enforce")]
        assert (pins[0]["scope"], pins[0]["folder_id"]) == ("platform", None)


async def test_pins_are_ordered_by_holder_depth_name_id_mode_and_version_like_the_report(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        org_pm = stack.policy_manager(chain.org)
        root_pm = stack.policy_manager(stack.root_id)
        await stack.mandatory_rule_set(chain.org, name="Zeta", token=org_pm)
        beta = await stack.mandatory_rule_set(chain.org, name="Beta", token=org_pm)
        await stack.publish_rule_version(
            beta, org_pm, {"unique-property-names": stack.rule_body("Beta v2")}
        )
        await stack.mandatory_rule_set(stack.root_id, name="Root rules", token=root_pm)
        ed = await editor(stack, chain.team)

        draft = await publish_ok(stack, chain.team, ed)

        pins = pins_of_row(version_row(stack, sql, draft))
        assert short(pins) == [
            *SEMANTIC_PINS,
            ("odcs-structural", 1, "mandatory", "enforce"),
            ("Root rules", 1, "mandatory", "enforce"),
            ("Beta", 1, "mandatory", "enforce"),
            ("Beta", 2, "mandatory", "warn"),
            ("Zeta", 1, "mandatory", "enforce"),
        ]


async def test_the_database_refuses_every_later_change_of_the_pins_after_publish(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        ed = await editor(stack, chain.team)
        draft = await publish_ok(stack, chain.team, ed)
        before = version_row(stack, sql, draft)

        for column, value in (
            ("pinned_rule_sets", "'[]'::jsonb"),
            ("validation_run_id", f"'{uuid.uuid4()}'::uuid"),
            ("engine_version", "'0.0.0'"),
            ("schema_sha", "'tampered'"),
        ):
            with pytest.raises(psycopg.Error, match="immutable"):
                sql(
                    stack.contracts_db_url,
                    f"UPDATE contract_version SET {column} = {value} WHERE id = %s",  # noqa: S608
                    draft,
                )

        after = version_row(stack, sql, draft)
        for column in ("pinned_rule_sets", "validation_run_id", "engine_version", "schema_sha"):
            assert after[column] == before[column]
        # The lifecycle columns still move (the guard's lifecycle list is unchanged).
        sql(
            stack.contracts_db_url,
            "UPDATE contract_version SET deprecated_at = now(), "
            "sunset_date = current_date + 30 WHERE id = %s",
            draft,
        )


async def test_migration_0014_downgrades_to_the_0005_guard_and_upgrades_again(
    fresh_db_url: str, sql: Sql
) -> None:
    from dc_contracts.migrate import SCRIPT_LOCATION, VERSION_TABLE
    from dc_core.migrations import run_alembic

    def migrate(command: str, revision: str = "head") -> None:
        run_alembic(
            script_location=SCRIPT_LOCATION,
            version_table=VERSION_TABLE,
            url=fresh_db_url,
            command=command,  # type: ignore[arg-type]
            revision=revision,
        )

    def guard() -> str:
        return str(
            sql(fresh_db_url, "SELECT pg_get_functiondef('contract_version_guard'::regproc) AS d")[
                0
            ]["d"]
        )

    def pin_columns() -> list[str]:
        rows = sql(
            fresh_db_url,
            "SELECT column_name FROM information_schema.columns WHERE table_name = "
            "'contract_version' AND column_name IN ('validation_run_id', 'pinned_rule_sets', "
            "'engine_version', 'schema_sha') ORDER BY column_name",
        )
        return [r["column_name"] for r in rows]

    migrate("upgrade")
    assert "'validation_run_id', 'pinned_rule_sets', 'engine_version', 'schema_sha'" in guard()
    assert len(pin_columns()) == 4
    migrate("downgrade", "0013_card_webhook_secret")
    assert "pinned_rule_sets" not in guard()
    assert "lifecycle_mutable" in guard()
    assert pin_columns() == []
    migrate("upgrade")
    assert "pinned_rule_sets" in guard()
    assert len(pin_columns()) == 4


# -- a consumer sees what a version was validated against (D-132, RULE-07) ----------------------

PIN_FIELDS = ("validation_run_id", "pinned_rule_sets", "engine_version", "schema_sha")


async def test_a_consumer_sees_the_pins_of_a_version_in_its_metadata_and_in_the_list(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        contract, ids = await stack.publish_versions(chain.team, ["1.0.0"])
        consumer, principal = stack.token("consumer")
        stack.grant_sql(principal, "consumer", chain.team)
        row = version_row(stack, sql, ids["1.0.0"])
        base = f"{CONTRACTS}/{contract}/versions"

        meta = await stack.client.get(f"{base}/1.0.0/metadata", headers=bearer(consumer))
        listed = await stack.client.get(base, headers=bearer(consumer))

        assert meta.status_code == 200, meta.text
        body = meta.json()
        assert body["validation_run_id"] == str(row["validation_run_id"])
        assert body["pinned_rule_sets"] == row["pinned_rule_sets"]
        assert [pin["name"] for pin in body["pinned_rule_sets"]] == PLATFORM_PIN_NAMES
        assert body["engine_version"] == row["engine_version"]
        assert body["schema_sha"] == row["schema_sha"]
        assert body["engine_version"] and body["schema_sha"]
        assert listed.status_code == 200, listed.text
        assert [{k: i[k] for k in PIN_FIELDS} for i in listed.json()["items"]] == [
            {k: body[k] for k in PIN_FIELDS}
        ]


async def test_a_version_published_before_the_phase_shows_null_pins(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, ids = await stack.publish_versions(chain.team, ["1.0.0", "1.1.0"])
        # Make 1.1.0 look like a version published before migration 0014 (no pins were recorded).
        sql(stack.contracts_db_url, "ALTER TABLE contract_version DISABLE TRIGGER USER")
        sql(
            stack.contracts_db_url,
            "UPDATE contract_version SET validation_run_id = NULL, pinned_rule_sets = NULL, "
            "engine_version = NULL, schema_sha = NULL WHERE id = %s",
            ids["1.1.0"],
        )
        sql(stack.contracts_db_url, "ALTER TABLE contract_version ENABLE TRIGGER USER")
        consumer, principal = stack.token("consumer")
        stack.grant_sql(principal, "consumer", chain.team)
        base = f"{CONTRACTS}/{contract}/versions"

        old = await stack.client.get(f"{base}/1.1.0/metadata", headers=bearer(consumer))
        new = await stack.client.get(f"{base}/1.0.0/metadata", headers=bearer(consumer))

        assert old.status_code == 200, old.text
        assert {k: old.json()[k] for k in PIN_FIELDS} == dict.fromkeys(PIN_FIELDS)
        assert new.json()["validation_run_id"] is not None
        assert new.json()["pinned_rule_sets"] == []


# -- a released version needs ODCS status: active (quick 261128-rsa) ----------------------------

PUBLICATION_SET = "8285c164-893d-4088-bd46-c517186d3d5f"  # seed.PLATFORM_ODCS_PUBLICATION_ID
RELEASED_KEY = "released-status-active"
PREVIEW_KEY = "published-status-active"


def publication_findings(report: dict[str, Any]) -> list[tuple[int, str, str, bool]]:
    """(rule-set version, rule key, mode, blocking) of every odcs-publication finding."""
    return [
        (f["rule_set"]["version"], f["rule_key"], f["mode"], f["blocking"])
        for f in report["findings"]
        if f["rule_set"]["name"] == "odcs-publication"
    ]


async def stored_report(stack: Any, contract: str, semver: str, token: str) -> dict[str, Any]:
    response = await stack.client.get(
        f"{CONTRACTS}/{contract}/versions/{semver}/validation-report", headers=bearer(token)
    )
    assert response.status_code == 200, response.text
    report: dict[str, Any] = response.json()["report"]
    return report


async def test_a_released_version_without_status_active_warns_in_grace_and_is_blocked_after(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack(platform_seed=True) as stack:
        chain = await stack.chain()
        ed = await editor(stack, chain.team)

        # In warn grace (version 2's enforce_at is DC_MANDATORY_WARN_DAYS ahead): it publishes,
        # and the stored report carries the non-blocking error next to the version 1 warning.
        graced_id = new_id()
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(graced_id, "id", status="draft"), token=ed
        )
        graced = await stack.publish(contract, draft, etag, token=ed)
        assert graced.status_code == 201, graced.text
        report = await stored_report(stack, contract, "1.0.0", ed)
        assert publication_findings(report) == [
            (1, PREVIEW_KEY, "warn", False),
            (2, RELEASED_KEY, "warn", False),
        ]

        # A Global admin enforces version 2 now through the ROOT platform route.
        enforced = await stack.validation.post(
            f"/api/v1/validation/folders/{stack.root_id}/platform-rule-sets/"
            f"{PUBLICATION_SET}/versions/2/enforce-now",
            headers=bearer(stack.admin_token),
        )
        assert enforced.status_code == 200, enforced.text

        odcs_id = new_id()
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(odcs_id, "id", status="draft"), token=ed
        )
        events_before = published_events(stack, sql)
        refused = await stack.publish(contract, draft, etag, token=ed)

        assert refused.status_code == 422, refused.text
        problem = refused.json()
        assert problem["type"] == "urn:dc:problem:contract-validation-failed"
        assert publication_findings(problem["report"]) == [(2, RELEASED_KEY, "enforce", True)]
        assert version_row(stack, sql, draft)["state"] == "draft"
        assert published_events(stack, sql) == events_before

        # A missing status is refused the same way.
        no_status = doc(odcs_id, "id", status="draft").replace(b"status: draft\n", b"")
        etag = await save(stack, contract, draft, etag, no_status, ed)
        missing = await stack.publish(contract, draft, etag, token=ed)
        assert missing.status_code == 422, missing.text
        assert publication_findings(missing.json()["report"]) == [
            (2, RELEASED_KEY, "enforce", True)
        ]

        # A prerelease publishes as a Preview with the warning only.
        preview_body = doc(odcs_id, "id", version="1.0.0-rc.1", status="draft")
        etag = await save(stack, contract, draft, etag, preview_body, ed)
        preview = await stack.publish(contract, draft, etag, token=ed)
        assert preview.status_code == 201, preview.text
        report = await stored_report(stack, contract, "1.0.0-rc.1", ed)
        assert publication_findings(report) == [(2, PREVIEW_KEY, "enforce", False)]

        # The fixed release (status: active) publishes with no publication finding.
        started = await stack.client.post(
            f"{CONTRACTS}/{contract}/drafts",
            json={"from_version": "1.0.0-rc.1"},
            headers=bearer(ed),
        )
        assert started.status_code == 201, started.text
        next_draft = started.json()["draft"]
        etag = await save(
            stack,
            contract,
            next_draft["id"],
            f'"r{next_draft["revision"]}"',
            doc(odcs_id, "id", version="1.0.0"),
            ed,
        )
        released = await stack.publish(contract, next_draft["id"], etag, token=ed)
        assert released.status_code == 201, released.text
        report = await stored_report(stack, contract, "1.0.0", ed)
        assert publication_findings(report) == []


# -- WR-04: no pooled connection sits idle in transaction across the gate call --------------------

IDLE_IN_TX = (
    "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database() "
    "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%%'"
)


async def test_no_contracts_connection_is_idle_in_transaction_during_the_gate_call(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        ed = await editor(stack, chain.team)
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(new_id(), "id", "name"), token=ed
        )
        seen: list[int] = []

        async def probe(request: Any) -> None:
            if request.url.path.endswith("/runs"):
                seen.append(int(sql(stack.contracts_db_url, IDLE_IN_TX)[0]["n"]))

        stack.validation_link.on_request = probe
        published = await stack.publish(contract, draft, etag, token=ed)

        assert published.status_code == 201, published.text
        assert seen == [0]  # the gate was called once and no connection was held across it
