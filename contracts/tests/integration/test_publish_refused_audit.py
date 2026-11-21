"""A publish the gate refuses is audited as ``contract.version.publish_refused`` (D-175, AUD-05).

The client always receives the gate's 422 ``contract-validation-failed`` unchanged. After the
refused unit is rolled back, contracts records one change event on the contract in its own
transaction: the draft id, the declared version (capped), the validation run id and the blocking
``{rule_key, code}`` pairs. Never a finding message, pointer, path or any document value
(D-148 allow-list, T-7-07).
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

ACTION = "contract.version.publish_refused"
# A property name that only the draft document holds: it must never reach the event.
SECRET_COLUMN = "secretcolumnxyz"
SECRET_MESSAGE = "TOPSECRET-finding-message"


def doc(odcs_id: str, *names: str, version: str = "1.0.0") -> bytes:
    """A conformant ODCS v3.2.0 document with one table holding the named properties."""
    props = "".join(f"      - name: {n}\n        logicalType: string\n" for n in names)
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        f"status: draft\nname: Orders\nschema:\n  - name: orders\n    properties:\n{props}"
    ).encode()


def new_id() -> str:
    return f"urn:dc:refused:{uuid.uuid4().hex}"


def finding(rule_key: str, *, blocking: bool, error_code: str | None = None) -> dict[str, Any]:
    """A report finding whose free-text fields quote the document (they must stay out)."""
    return {
        "rule_set": {
            "id": None,
            "name": None,
            "folder_id": None,
            "folder_kind": None,
            "scope": None,
            "version": None,
        },
        "source": "mandatory",
        "mode": "enforce",
        "enforce_at": None,
        "rule_key": rule_key,
        "severity": "error" if blocking else "warning",
        "outcome": "error" if error_code else "fail",
        "message": f"{SECRET_MESSAGE} {SECRET_COLUMN}",
        "location": {
            "pointer": f"/schema/0/properties/{SECRET_COLUMN}",
            "path": f"$.schema[0].{SECRET_COLUMN}",
        },
        "blocking": blocking,
        "error_code": error_code,
    }


def refuse_with(findings: list[dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """A ``ValidationLink.rewrite`` turning the real answer into a refusal with ``findings``."""

    def _rewrite(answer: dict[str, Any]) -> dict[str, Any]:
        report = answer["report"]
        report["findings"] = findings
        report["findings_total"] = len(findings)
        report["summary"] = {
            "publishable": False,
            "blocking_count": sum(1 for f in findings if f["blocking"]),
        }
        return answer

    return _rewrite


def refused_events(stack: Any, sql: Sql) -> list[dict[str, Any]]:
    return sql(
        stack.contracts_db_url,
        "SELECT action, aggregate_type, aggregate_id, scope_path, before, after FROM outbox "
        "WHERE action = %s ORDER BY id",
        ACTION,
    )


def ancestors(stack: Any, sql: Sql, contract: str) -> list[str]:
    rows = sql(stack.contracts_db_url, "SELECT ancestor_ids FROM contract WHERE id = %s", contract)
    return [str(i) for i in rows[0]["ancestor_ids"]]


async def _draft(stack: Any, *names: str, version: str = "1.0.0") -> tuple[str, str, str]:
    chain = await stack.chain()
    return await stack.create_contract(  # type: ignore[no-any-return]
        chain.team, doc(new_id(), *names, version=version)
    )


# -- tracer ---------------------------------------------------------------------------------------


async def test_a_refused_publish_records_one_value_free_event_and_the_client_gets_the_422(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        contract, draft, etag = await _draft(stack, "id", SECRET_COLUMN)
        stack.validation_link.rewrite = refuse_with(
            [
                finding("r-a", blocking=True),
                finding("r-b", blocking=True, error_code="no_such_key"),
                finding("r-c", blocking=False),
            ]
        )

        refused = await stack.publish(contract, draft, etag)

        assert refused.status_code == 422, refused.text
        problem = refused.json()
        assert problem["type"] == "urn:dc:problem:contract-validation-failed"
        # The gate's body reaches the client unchanged: the full report, messages included.
        assert [f["rule_key"] for f in problem["report"]["findings"]] == ["r-a", "r-b", "r-c"]
        assert problem["report"]["findings"][0]["message"].startswith(SECRET_MESSAGE)
        run_id = problem["run_id"]
        assert run_id

        events = refused_events(stack, sql)
        assert len(events) == 1
        event = events[0]
        assert event["aggregate_type"] == "contract"
        assert event["aggregate_id"] == contract
        assert [str(i) for i in event["scope_path"]] == ancestors(stack, sql, contract)
        assert event["before"] is None
        assert event["after"] == {
            "draft_id": draft,
            "semver": "1.0.0",
            "run_id": run_id,
            "blocking": [
                {"rule_key": "r-a", "code": "fail"},
                {"rule_key": "r-b", "code": "no_such_key"},
            ],
        }
        serialised = json.dumps(event, default=str)
        assert SECRET_MESSAGE not in serialised
        assert SECRET_COLUMN not in serialised
        assert "/schema" not in serialised
        assert "$." not in serialised

        # The draft is untouched and no version was published.
        row = sql(stack.contracts_db_url, "SELECT * FROM contract_version WHERE id = %s", draft)[0]
        assert row["state"] == "draft"
        assert row["revision"] == 1
        published = sql(
            stack.contracts_db_url,
            "SELECT count(*) AS n FROM contract_version WHERE contract_id = %s "
            "AND state <> 'draft'",
            contract,
        )
        assert published[0]["n"] == 0


# -- helpers for the later cases ------------------------------------------------------------------

CONTRACTS = "/api/v1/contracts/contracts"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def new_draft(
    stack: Any, contract: str, odcs_id: str, version: str, names: tuple[str, ...], *, base: str
) -> tuple[str, str]:
    """A new draft copied from ``base`` and saved with the given version and columns."""
    created = await stack.client.post(
        f"{CONTRACTS}/{contract}/drafts",
        json={"from_version": base, "bump": "none"},
        headers=bearer(stack.admin_token),
    )
    assert created.status_code == 201, created.text
    draft = created.json()["draft"]
    saved = await stack.client.put(
        f"{CONTRACTS}/{contract}/drafts/{draft['id']}",
        content=doc(odcs_id, *names, version=version),
        headers={
            **bearer(stack.admin_token),
            "Content-Type": "application/yaml",
            "If-Match": draft["etag"],
        },
    )
    assert saved.status_code == 200, saved.text
    return str(draft["id"]), str(saved.headers["etag"])


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


# -- the native semver finding, the caps ----------------------------------------------------------


async def test_a_semver_breaking_refusal_records_the_native_finding(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_id()
        contract, first, etag = await stack.create_contract(
            chain.team, doc(odcs_id, "id", "amount")
        )
        assert (await stack.publish(contract, first, etag)).status_code == 201
        draft, draft_etag = await new_draft(
            stack, contract, odcs_id, "1.1.0", ("id",), base="1.0.0"
        )

        refused = await stack.publish(contract, draft, draft_etag)

        assert refused.status_code == 422, refused.text
        assert refused.json()["type"] == "urn:dc:problem:contract-validation-failed"
        events = refused_events(stack, sql)
        assert len(events) == 1
        after = events[0]["after"]
        assert after["draft_id"] == draft
        assert after["semver"] == "1.1.0"
        assert after["run_id"] == refused.json()["run_id"]
        assert {"rule_key": "semver-breaking-change", "code": "fail"} in after["blocking"]
        assert "amount" not in json.dumps(events[0], default=str)


async def test_the_blocking_list_holds_at_most_50_pairs_and_the_version_at_most_256_chars(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        long_version = "1.0.0-" + "a" * 300
        contract, draft, etag = await _draft(stack, "id", version=long_version)
        stack.validation_link.rewrite = refuse_with(
            [finding(f"r-{i:02d}", blocking=True) for i in range(60)]
        )

        refused = await stack.publish(contract, draft, etag)

        assert refused.status_code == 422, refused.text
        assert len(refused.json()["report"]["findings"]) == 60
        after = refused_events(stack, sql)[0]["after"]
        assert after["blocking"] == [{"rule_key": f"r-{i:02d}", "code": "fail"} for i in range(50)]
        assert after["semver"] == long_version[:256]


async def test_a_schema_only_refusal_records_the_structural_pair_and_no_pointer(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        contract, draft, etag = await _draft(stack, "id", SECRET_COLUMN)

        def schema_invalid(answer: dict[str, Any]) -> dict[str, Any]:
            report = refuse_with([])(answer)["report"]
            report["structural"] = {
                "valid": False,
                "error_count": 1,
                "errors": [
                    {
                        "pointer": f"/schema/0/properties/{SECRET_COLUMN}",
                        "keyword": "type",
                        "message": "The value has the wrong type.",
                    }
                ],
            }
            return answer

        stack.validation_link.rewrite = schema_invalid

        refused = await stack.publish(contract, draft, etag)

        assert refused.status_code == 422, refused.text
        events = refused_events(stack, sql)
        assert len(events) == 1
        assert events[0]["after"]["blocking"] == [
            {"rule_key": "odcs-json-schema", "code": "structural"}
        ]
        assert SECRET_COLUMN not in json.dumps(events[0], default=str)


# -- a recorder failure never changes the answer --------------------------------------------------


async def test_a_failing_recorder_still_answers_the_422_and_logs_ids_only(
    make_stack: MakeStack, sql: Sql
) -> None:
    from structlog.testing import capture_logs

    async with make_stack() as stack:
        contract, draft, etag = await _draft(stack, "id", SECRET_COLUMN)
        sql(
            stack.contracts_db_url,
            "CREATE FUNCTION refuse_publish_refused() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF NEW.action = 'contract.version.publish_refused' THEN "
            "RAISE EXCEPTION 'outbox down'; END IF; RETURN NEW; END $$",
        )
        sql(
            stack.contracts_db_url,
            "CREATE TRIGGER refuse_publish_refused BEFORE INSERT ON outbox "
            "FOR EACH ROW EXECUTE FUNCTION refuse_publish_refused()",
        )
        stack.validation_link.rewrite = refuse_with([finding("r-a", blocking=True)])

        with capture_logs() as logs:
            refused = await stack.publish(contract, draft, etag)

        assert refused.status_code == 422, refused.text
        assert refused.json()["type"] == "urn:dc:problem:contract-validation-failed"
        assert refused_events(stack, sql) == []
        warnings = [entry for entry in logs if entry["event"] == "publish_refused_not_recorded"]
        assert len(warnings) == 1
        warning = warnings[0]
        assert warning["log_level"] == "warning"
        assert warning["contract_id"] == contract
        assert warning["draft_id"] == draft
        assert set(warning) == {"event", "log_level", "contract_id", "draft_id", "error"}
        assert SECRET_COLUMN not in json.dumps(warning, default=str)

        # The session is healthy afterwards: once the outbox works again, a refusal records.
        sql(stack.contracts_db_url, "DROP TRIGGER refuse_publish_refused ON outbox")
        again = await stack.publish(contract, draft, etag)
        assert again.status_code == 422, again.text
        assert len(refused_events(stack, sql)) == 1


# -- only a gate verdict is a refusal -------------------------------------------------------------


async def test_no_other_publish_outcome_records_a_publish_refused_event(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_id()
        contract, first, etag = await stack.create_contract(chain.team, doc(odcs_id, "id"))
        # 428 without If-Match, 412 with a stale one, 404 for an unknown draft.
        assert (await stack.publish(contract, first, None)).status_code == 428
        assert (await stack.publish(contract, first, '"r99"')).status_code == 412
        assert (await stack.publish(contract, str(uuid.uuid4()), etag)).status_code == 404
        # 503: validation is down (an outage, not a verdict).
        stack.validation_link.down = True
        outage = await stack.publish(contract, first, etag)
        assert outage.status_code == 503, outage.text
        assert outage.json()["type"] == "urn:dc:problem:validation-unavailable"
        stack.validation_link.restore()
        # 201 twice: 1.0.0 (Latest) and 1.1.0 (Preview).
        assert (await stack.publish(contract, first, etag)).status_code == 201
        # 409 draft-already-published.
        again = await stack.publish(contract, first, etag)
        assert again.status_code == 409, again.text
        preview, preview_etag = await new_draft(
            stack, contract, odcs_id, "1.1.0", ("id",), base="1.0.0"
        )
        assert (await stack.publish(contract, preview, preview_etag)).status_code == 201
        draft, draft_etag = await new_draft(
            stack, contract, odcs_id, "1.2.0", ("id",), base="1.1.0"
        )

        async def unpublish_baseline() -> None:
            response = await stack.client.post(
                f"{CONTRACTS}/{contract}/versions/1.1.0/unpublish",
                headers=bearer(stack.admin_token),
            )
            assert response.status_code == 200, response.text

        async def bump_attachments() -> None:
            sql(
                stack.contracts_db_url,
                "UPDATE contract SET attachments_revision = attachments_revision + 1 WHERE id = %s",
                contract,
            )

        real_gate = stack.contracts_app.state.publish_gate
        try:
            stack.contracts_app.state.publish_gate = RacingGate(real_gate, unpublish_baseline)
            raced = await stack.publish(contract, draft, draft_etag)
            assert raced.status_code == 409, raced.text
            assert raced.json()["type"] == "urn:dc:problem:baseline-changed"
            stack.contracts_app.state.publish_gate = RacingGate(real_gate, bump_attachments)
            changed = await stack.publish(contract, draft, draft_etag)
            assert changed.status_code == 409, changed.text
            assert changed.json()["type"] == "urn:dc:problem:rules-changed"
        finally:
            stack.contracts_app.state.publish_gate = real_gate

        assert refused_events(stack, sql) == []


# -- the notice consumer ignores the action -------------------------------------------------------


async def test_the_notice_consumer_creates_no_card_notice_for_a_refused_publish(
    make_stack: MakeStack, sql: Sql
) -> None:
    from dc_contracts.models import OutboxEvent
    from dc_contracts.notices import handle_payload
    from dc_core.events import build_envelope, encode_envelope, subject_for
    from sqlalchemy import select

    async with make_stack() as stack:
        contract, draft, etag = await _draft(stack, "id")
        stack.validation_link.rewrite = refuse_with([finding("r-a", blocking=True)])
        assert (await stack.publish(contract, draft, etag)).status_code == 422
        factory = stack.contracts_app.state.session_factory
        async with factory() as session:
            row = (
                await session.execute(select(OutboxEvent).where(OutboxEvent.action == ACTION))
            ).scalar_one()
        envelope = build_envelope(row, service="contracts")

        recorded = await handle_payload(
            factory, encode_envelope(envelope), subject=subject_for("contracts", "contract", ACTION)
        )

        assert recorded == 0
        rows = sql(stack.contracts_db_url, "SELECT count(*) AS n FROM card_notice")
        assert rows[0]["n"] == 0
