"""Access events for contract content reads (AUD-04, D-171).

Every 200 that returns contract document content enqueues exactly one best-effort access event on
``access.contracts.contract.read``; a 304, an error and the metadata routes enqueue nothing. The
contracts app's ``access_log`` is replaced by an ``AccessLog`` over a recording publisher, so no
NATS server is needed.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
import structlog
from dc_audit.ingest import AuditEnvelope, category_for
from dc_contracts.access_events import DETAIL_KEYS
from dc_core.access import AccessLog
from dc_core.events import PublishAck

pytestmark = pytest.mark.integration

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]

SUBJECT = "access.contracts.contract.read"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def doc(odcs_id: str, *, version: str = "1.0.0") -> bytes:
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        "status: draft\nname: Orders\n"
        "schema:\n  - name: orders\n    properties:\n      - name: id\n"
        "        logicalType: string\n"
    ).encode()


def contract_url(contract: str) -> str:
    return f"/api/v1/contracts/contracts/{contract}"


def versions_url(contract: str, ref: str) -> str:
    return f"{contract_url(contract)}/versions/{ref}"


@dataclass
class RecordingPublisher:
    messages: list[tuple[str, bytes, str]] = field(default_factory=list)

    async def publish(self, subject: str, payload: bytes, *, msg_id: str) -> PublishAck:
        self.messages.append((subject, payload, msg_id))
        return PublishAck(duplicate=False)

    async def close(self) -> None:
        return None

    def envelopes(self) -> list[dict[str, Any]]:
        return [json.loads(payload) for _, payload, _ in self.messages]


@dataclass
class Recorder:
    publisher: RecordingPublisher
    log: AccessLog

    async def settle(self) -> list[dict[str, Any]]:
        """Wait until the queue drained and a little longer; the envelopes published so far."""
        for _ in range(100):
            if self.log.pending == 0:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.15)
        return self.publisher.envelopes()


@asynccontextmanager
async def recording(stack: Any) -> AsyncIterator[Recorder]:
    publisher = RecordingPublisher()
    log = AccessLog(publisher, flush_s=0.05)
    task = asyncio.create_task(log.run())
    stack.contracts_app.state.access_log = log
    try:
        yield Recorder(publisher, log)
    finally:
        stack.contracts_app.state.access_log = None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def published_contract(stack: Any, team: str) -> tuple[str, dict[str, Any]]:
    odcs_id = f"urn:dc:access-events:{uuid.uuid4().hex}"
    contract, draft, etag = await stack.create_contract(team, doc(odcs_id))
    published = await stack.publish(contract, draft, etag)
    assert published.status_code == 201, published.text
    return contract, dict(published.json())


async def consumer_at(stack: Any, team: str, sub: str = "access-cs") -> tuple[str, str]:
    token, principal = stack.token(sub)
    stack.grant_sql(principal, "consumer", team)
    return token, principal


async def test_fetching_latest_publishes_one_access_event_and_a_304_publishes_none(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, published = await published_contract(stack, chain.team)
        consumer, principal = await consumer_at(stack, chain.team)
        async with recording(stack) as recorder:
            fetched = await stack.client.get(
                versions_url(contract, "latest"), headers=bearer(consumer)
            )
            assert fetched.status_code == 200, fetched.text
            envelopes = await recorder.settle()
            assert len(envelopes) == 1
            subject, _, msg_id = recorder.publisher.messages[0]
            assert subject == SUBJECT
            envelope = envelopes[0]
            assert msg_id == envelope["event_id"]
            assert envelope["service"] == "contracts"
            assert envelope["category"] == "access"
            assert envelope["action"] == "read"
            assert envelope["aggregate_type"] == "contract"
            assert envelope["aggregate_id"] == contract
            assert envelope["scope_path"] == [
                stack.root_id,
                chain.org,
                chain.project,
                chain.team,
            ]
            assert envelope["actor"]["id"] == principal
            assert envelope["before"] is None
            assert envelope["after"] is None
            assert envelope["request_id"] == fetched.headers["x-request-id"]
            assert envelope["detail"] == {
                "contract_id": contract,
                "version_id": published["version_id"],
                "semver": "1.0.0",
                "ref": "latest",
                "media_type": "application/yaml",
            }
            assert set(envelope["detail"]) <= DETAIL_KEYS
            # The envelope is one the audit service stores as an access row.
            model = AuditEnvelope.model_validate(envelope)
            assert category_for(subject, model) == "access"
            # No document content travels with the event.
            assert b"Orders" not in recorder.publisher.messages[0][1]

            revalidated = await stack.client.get(
                versions_url(contract, "latest"),
                headers={**bearer(consumer), "If-None-Match": fetched.headers["etag"]},
            )
            assert revalidated.status_code == 304
            assert len(await recorder.settle()) == 1


async def test_the_head_and_draft_documents_and_a_converted_version_each_publish_one_event(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, published = await published_contract(stack, chain.team)
        reader, reader_id = stack.token("access-reader")
        stack.grant_sql(reader_id, "team_reader", chain.team)
        editor, editor_id = stack.token("access-editor")
        stack.grant_sql(editor_id, "team_editor", chain.team)
        created = await stack.client.post(
            f"{contract_url(contract)}/drafts",
            json={"from_version": "1.0.0", "bump": "minor"},
            headers=bearer(stack.admin_token),
        )
        assert created.status_code == 201, created.text
        draft_id = created.json()["draft"]["id"]
        async with recording(stack) as recorder:
            head = await stack.client.get(
                f"{contract_url(contract)}/document", headers=bearer(reader)
            )
            assert head.status_code == 200, head.text
            envelopes = await recorder.settle()
            assert len(envelopes) == 1
            assert envelopes[0]["actor"]["id"] == reader_id
            head_detail = envelopes[0]["detail"]
            assert head_detail["contract_id"] == contract
            assert head_detail["ref"] == "head"
            assert head_detail["media_type"] == head.headers["content-type"]
            assert "version_id" in head_detail
            assert "draft_id" not in head_detail

            draft = await stack.client.get(
                f"{contract_url(contract)}/drafts/{draft_id}/document", headers=bearer(editor)
            )
            assert draft.status_code == 200, draft.text
            envelopes = await recorder.settle()
            assert len(envelopes) == 2
            assert envelopes[1]["actor"]["id"] == editor_id
            assert envelopes[1]["detail"] == {
                "contract_id": contract,
                "draft_id": draft_id,
                "ref": "draft",
                "media_type": "application/yaml",
            }

            converted = await stack.client.get(
                versions_url(contract, "1.0.0"),
                headers={**bearer(reader), "Accept": "application/json"},
            )
            assert converted.status_code == 200, converted.text
            assert converted.headers["content-type"] == "application/json"
            envelopes = await recorder.settle()
            assert len(envelopes) == 3
            assert envelopes[2]["detail"] == {
                "contract_id": contract,
                "version_id": published["version_id"],
                "semver": "1.0.0",
                "ref": "1.0.0",
                "media_type": "application/json",
            }
            for envelope in envelopes:
                assert set(envelope["detail"]) <= DETAIL_KEYS
                assert envelope["before"] is None
                assert envelope["after"] is None
                model = AuditEnvelope.model_validate(envelope)
                assert category_for(SUBJECT, model) == "access"
            assert {subject for subject, _, _ in recorder.publisher.messages} == {SUBJECT}


async def test_create_from_version_publishes_one_access_event_on_the_source_contract(
    make_stack: MakeStack,
) -> None:
    """WR-04: a copy reads the source's content, so the source's auditors see who copied it."""
    async with make_stack() as stack:
        source_chain = await stack.chain("acc-src")
        target_chain = await stack.chain("acc-dst")
        contract, published = await published_contract(stack, source_chain.team)
        created = await stack.client.post(
            f"{contract_url(contract)}/drafts",
            json={"from_version": "1.0.0", "bump": "minor"},
            headers=bearer(stack.admin_token),
        )
        assert created.status_code == 201, created.text
        draft_id = created.json()["draft"]["id"]
        copier, copier_id = stack.token("access-copier")
        stack.grant_sql(copier_id, "team_editor", target_chain.team)
        stack.grant_sql(copier_id, "team_reader", source_chain.team)
        from_version = f"/api/v1/contracts/folders/{target_chain.team}/contracts/from-version"
        source_scope = [stack.root_id, source_chain.org, source_chain.project, source_chain.team]

        # A client-chosen id: the in-process validation call made after the event (the save-mode
        # embed) rebinds this process's correlation id, so the response header is not the one.
        request_id = str(uuid.uuid4())
        async with recording(stack) as recorder:
            copied = await stack.client.post(
                from_version,
                json={
                    "source_contract_id": contract,
                    "source_version": "1.0.0",
                    "odcs_id": f"urn:dc:access-copy:{uuid.uuid4().hex}",
                },
                headers={**bearer(copier), "X-Request-ID": request_id},
            )
            assert copied.status_code == 201, copied.text
            envelopes = await recorder.settle()
            assert len(envelopes) == 1, envelopes
            envelope = envelopes[0]
            assert envelope["aggregate_id"] == contract
            assert envelope["scope_path"] == source_scope
            assert envelope["actor"]["id"] == copier_id
            assert uuid.UUID(envelope["request_id"]) == uuid.UUID(request_id)
            assert envelope["detail"] == {
                "contract_id": contract,
                "version_id": published["version_id"],
                "semver": "1.0.0",
                "ref": "1.0.0",
                "media_type": "application/yaml",
            }
            assert b"Orders" not in recorder.publisher.messages[0][1]

            from_draft = await stack.client.post(
                from_version,
                json={
                    "source_contract_id": contract,
                    "source_draft_id": draft_id,
                    "odcs_id": f"urn:dc:access-copy:{uuid.uuid4().hex}",
                },
                headers=bearer(copier),
            )
            assert from_draft.status_code == 201, from_draft.text
            envelopes = await recorder.settle()
            assert len(envelopes) == 2, envelopes
            assert envelopes[1]["aggregate_id"] == contract
            assert envelopes[1]["scope_path"] == source_scope
            assert envelopes[1]["detail"] == {
                "contract_id": contract,
                "draft_id": draft_id,
                "ref": "draft",
                "media_type": "application/yaml",
            }

            refused = await stack.client.post(
                from_version,
                json={
                    "source_contract_id": contract,
                    "source_version": "9.9.9",
                    "odcs_id": f"urn:dc:access-copy:{uuid.uuid4().hex}",
                },
                headers=bearer(copier),
            )
            assert refused.status_code == 404, refused.text
            assert len(await recorder.settle()) == 2


async def test_metadata_list_search_diff_report_and_template_reads_publish_nothing(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await published_contract(stack, chain.team)
        admin = bearer(stack.admin_token)
        created = await stack.client.post(
            f"{contract_url(contract)}/drafts",
            json={"from_version": "1.0.0", "bump": "minor"},
            headers=admin,
        )
        assert created.status_code == 201, created.text
        draft_id = created.json()["draft"]["id"]
        template = await stack.client.post(
            f"/api/v1/contracts/folders/{chain.org}/templates",
            params={"name": "Access template"},
            content=doc(f"urn:dc:access-template:{uuid.uuid4().hex}"),
            headers={**admin, "Content-Type": "application/yaml"},
        )
        assert template.status_code == 201, template.text
        template_id = template.json()["template"]["id"]
        async with recording(stack) as recorder:
            reads = {
                "version metadata": f"{versions_url(contract, 'latest')}/metadata",
                "versions list": f"{contract_url(contract)}/versions",
                "contract metadata": contract_url(contract),
                "search": "/api/v1/contracts/contracts?q=Orders",
                "diff": f"{contract_url(contract)}/diff?from=1.0.0&to=latest",
                "validation report": f"{versions_url(contract, 'latest')}/validation-report",
                "draft metadata": f"{contract_url(contract)}/drafts/{draft_id}",
                "template document": f"/api/v1/contracts/templates/{template_id}/document",
            }
            for name, url in reads.items():
                response = await stack.client.get(url, headers=admin)
                # The report may be unrecorded (404); every other read succeeds.
                allowed = {200, 404} if name == "validation report" else {200}
                assert response.status_code in allowed, (name, response.text)
            assert await recorder.settle() == []


async def test_refused_and_failed_content_reads_publish_nothing(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await published_contract(stack, chain.team)
        consumer, _ = await consumer_at(stack, chain.team)
        unknown = "019aaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"
        async with recording(stack) as recorder:
            attempts = [
                # a Consumer cannot read the head document: the uniform 404
                (f"{contract_url(contract)}/document", bearer(consumer), 404),
                (f"{contract_url(unknown)}/document", bearer(stack.admin_token), 404),
                (versions_url(unknown, "latest"), bearer(consumer), 404),
                (versions_url(contract, "9.9.9"), bearer(consumer), 404),
                (versions_url(contract, "latest"), {}, 401),
                (
                    versions_url(contract, "latest"),
                    {**bearer(consumer), "Accept": "text/html"},
                    406,
                ),
            ]
            for url, headers, status in attempts:
                response = await stack.client.get(url, headers=headers)
                assert response.status_code == status, (url, response.text)
            assert await recorder.settle() == []


async def test_without_an_access_log_every_content_route_still_answers(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        assert stack.contracts_app.state.access_log is None  # no NATS configured
        chain = await stack.chain()
        contract, _ = await published_contract(stack, chain.team)
        created = await stack.client.post(
            f"{contract_url(contract)}/drafts",
            json={"from_version": "1.0.0", "bump": "minor"},
            headers=bearer(stack.admin_token),
        )
        assert created.status_code == 201, created.text
        draft_id = created.json()["draft"]["id"]
        for url in (
            versions_url(contract, "latest"),
            f"{contract_url(contract)}/document",
            f"{contract_url(contract)}/drafts/{draft_id}/document",
        ):
            response = await stack.client.get(url, headers=bearer(stack.admin_token))
            assert response.status_code == 200, (url, response.text)


class ExplodingLog:
    def record(self, subject: str, envelope: dict[str, Any]) -> None:
        raise RuntimeError("queue broken")


async def test_a_failing_access_log_never_changes_the_read(make_stack: MakeStack) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _ = await published_contract(stack, chain.team)
        stack.contracts_app.state.access_log = ExplodingLog()
        try:
            with structlog.testing.capture_logs() as captured:
                response = await stack.client.get(
                    versions_url(contract, "latest"), headers=bearer(stack.admin_token)
                )
        finally:
            stack.contracts_app.state.access_log = None
        assert response.status_code == 200, response.text
        failures = [e for e in captured if e["event"] == "access_event_not_recorded"]
        assert len(failures) == 1
        assert failures[0]["error"] == "RuntimeError"
        assert set(failures[0]) <= {"event", "error", "log_level"}
