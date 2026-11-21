"""Concurrency suite for the version lifecycle (VER-08, D-40, D-50; the Phase 3 exit gate).

Every scenario sends real parallel requests (``asyncio.gather`` through ``httpx.ASGITransport``)
into one contracts app on real PostgreSQL, each request on its own pooled connection, and ends
with the research invariant probe: one Latest per contract with the flag and the pointer in
agreement, and no two non-draft rows sharing a semver.

Synchronisation uses events (``PausingGate``), never sleeps, so a scenario is deterministic about
the interleaving it claims to test. The engine pool allows 5 + 10 overflow connections, so no
scenario runs more than 12 requests at once (more would measure pool waits, not locking).
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from dc_contracts.settings import ContractsSettings
from dc_contracts.validation_client import GateContext, GateResult
from dc_contracts.versions import PassThroughPublishGate

pytestmark = pytest.mark.integration

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]
Invariant = Callable[[str], list[dict[str, Any]]]

CONTRACTS = "/api/v1/contracts/contracts"
ENTRY_TIMEOUT = 30.0


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def new_odcs_id() -> str:
    return f"urn:dc:race:{uuid.uuid4().hex}"


def doc(odcs_id: str, *, version: str = "1.0.0", name: str = "Orders") -> bytes:
    return (
        f'apiVersion: v3.2.0\nkind: DataContract\nid: {odcs_id}\nversion: "{version}"\n'
        f"status: draft\nname: {name}\n"
        "schema:\n  - name: orders\n    properties:\n      - name: id\n"
        "        logicalType: string\n"
    ).encode()


def drafts_url(contract: str) -> str:
    return f"{CONTRACTS}/{contract}/drafts"


def semver_key(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def problem_slug(response: Any) -> str:
    return str(response.json()["type"]).removeprefix("urn:dc:problem:")


async def new_draft(
    stack: Any, contract: str, odcs_id: str, version: str, *, source: dict[str, Any]
) -> tuple[str, str]:
    """A new draft started from ``source`` and edited to declare ``version``; (draft id, ETag)."""
    created = await stack.client.post(
        drafts_url(contract),
        json={**source, "bump": "none"},
        headers=bearer(stack.admin_token),
    )
    assert created.status_code == 201, created.text
    draft = created.json()["draft"]
    saved = await stack.client.put(
        f"{drafts_url(contract)}/{draft['id']}",
        content=doc(odcs_id, version=version),
        headers={
            **bearer(stack.admin_token),
            "Content-Type": "application/yaml",
            "If-Match": draft["etag"],
        },
    )
    assert saved.status_code == 200, saved.text
    return str(draft["id"]), str(saved.headers["etag"])


def published_events(stack: Any, sql: Sql) -> int:
    rows = sql(
        stack.contracts_db_url,
        "SELECT count(*) AS n FROM outbox WHERE action = 'contract.version_published'",
    )
    return int(rows[0]["n"])


class PausingGate:
    """A publish gate that holds every publish between the gate and the lock until released.

    Wraps the pass-through gate. ``entered`` is set when a publish reaches the gate, ``release``
    lets it continue, and ``checked`` records the content hash of every document it let through,
    so a test can compare the bytes that were checked with the bytes that were published.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.checked: list[str] = []
        self._inner = PassThroughPublishGate()

    async def check(
        self, raw: bytes, media_type: str, settings: ContractsSettings, ctx: GateContext
    ) -> GateResult:
        self.entered.set()
        await self.release.wait()
        result = await self._inner.check(raw, media_type, settings, ctx)
        self.checked.append(result.parsed.content_sha256)
        return result


async def test_concurrency_same_semver_publishes_yield_exactly_one_version(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_odcs_id()
        contract, first, etag = await stack.create_contract(chain.team, doc(odcs_id))
        assert (await stack.publish(contract, first, etag)).status_code == 201

        drafts = [
            await new_draft(stack, contract, odcs_id, "1.1.0", source={"from_version": "1.0.0"})
            for _ in range(8)
        ]
        results = await asyncio.gather(
            *[stack.publish(contract, draft, draft_etag) for draft, draft_etag in drafts]
        )

        statuses = sorted(r.status_code for r in results)
        assert statuses == [201] + [409] * 7, [r.text for r in results]
        assert {problem_slug(r) for r in results if r.status_code == 409} == {"semver-conflict"}
        winners = sql(
            stack.contracts_db_url,
            "SELECT count(*) AS n FROM contract_version "
            "WHERE contract_id = %s AND state <> 'draft' AND semver = '1.1.0'",
            contract,
        )
        assert winners[0]["n"] == 1
        latest = sql(
            stack.contracts_db_url,
            "SELECT count(*) AS n FROM contract_version WHERE contract_id = %s AND is_latest",
            contract,
        )
        assert latest[0]["n"] == 1
        assert invariant(stack.contracts_db_url) == []


async def test_concurrency_an_edit_during_the_gate_makes_publish_412_and_publishes_nothing(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_odcs_id()
        contract, draft, etag = await stack.create_contract(chain.team, doc(odcs_id))
        assert etag == '"r1"'
        events_before = published_events(stack, sql)
        edited = doc(odcs_id, name="Edited while the gate runs")

        gate = PausingGate()
        real_gate = stack.contracts_app.state.publish_gate
        stack.contracts_app.state.publish_gate = gate
        try:
            publishing = asyncio.create_task(stack.publish(contract, draft, '"r1"'))
            await asyncio.wait_for(gate.entered.wait(), ENTRY_TIMEOUT)

            saved = await stack.client.put(
                f"{drafts_url(contract)}/{draft}",
                content=edited,
                headers={
                    **bearer(stack.admin_token),
                    "Content-Type": "application/yaml",
                    "If-Match": '"r1"',
                },
            )
            assert saved.status_code == 200, saved.text
            assert saved.headers["etag"] == '"r2"'

            gate.release.set()
            refused = await asyncio.wait_for(publishing, ENTRY_TIMEOUT)
        finally:
            stack.contracts_app.state.publish_gate = real_gate

        assert refused.status_code == 412, refused.text
        assert problem_slug(refused) == "precondition-failed"
        row = sql(stack.contracts_db_url, "SELECT * FROM contract_version WHERE id = %s", draft)[0]
        assert row["state"] == "draft"
        assert row["revision"] == 2
        assert bytes(row["raw_document"]) == edited
        assert row["is_latest"] is False
        assert published_events(stack, sql) == events_before
        assert invariant(stack.contracts_db_url) == []


async def test_concurrency_without_an_edit_the_published_bytes_are_the_checked_bytes(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, etag = await stack.create_contract(chain.team, doc(new_odcs_id()))

        gate = PausingGate()
        real_gate = stack.contracts_app.state.publish_gate
        stack.contracts_app.state.publish_gate = gate
        try:
            publishing = asyncio.create_task(stack.publish(contract, draft, etag))
            await asyncio.wait_for(gate.entered.wait(), ENTRY_TIMEOUT)
            gate.release.set()
            published = await asyncio.wait_for(publishing, ENTRY_TIMEOUT)
        finally:
            stack.contracts_app.state.publish_gate = real_gate

        assert published.status_code == 201, published.text
        row = sql(stack.contracts_db_url, "SELECT * FROM contract_version WHERE id = %s", draft)[0]
        assert row["state"] == "published"
        assert gate.checked == [row["content_sha256"]]
        assert published.json()["content_sha256"] == row["content_sha256"]
        assert invariant(stack.contracts_db_url) == []


async def test_concurrency_first_publish_race_makes_the_first_committed_the_latest(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_odcs_id()
        contract, first, first_etag = await stack.create_contract(
            chain.team, doc(odcs_id, version="0.1.0")
        )
        drafts = [(first, first_etag)]
        for minor in range(2, 7):
            drafts.append(
                await new_draft(
                    stack, contract, odcs_id, f"0.{minor}.0", source={"from_draft_id": first}
                )
            )

        results = await asyncio.gather(
            *[stack.publish(contract, draft, draft_etag) for draft, draft_etag in drafts]
        )

        created = [r for r in results if r.status_code == 201]
        refused = [r for r in results if r.status_code != 201]
        assert created, [r.text for r in results]
        assert {r.status_code for r in refused} <= {409}, [r.text for r in results]
        # A racing publish that is above the winner now also answers 409 baseline-changed: it was
        # validated against the old baseline and a retry validates against the winner (D-147).
        assert {problem_slug(r) for r in refused} <= {"semver-conflict", "baseline-changed"}
        # The first commit is the Latest; every later one is above it (D-46), hence a Preview.
        assert sorted(r.json()["status"] for r in created) == ["latest"] + ["preview"] * (
            len(created) - 1
        )
        rows = sql(
            stack.contracts_db_url,
            "SELECT semver, is_latest FROM contract_version "
            "WHERE contract_id = %s AND state = 'published'",
            contract,
        )
        assert len(rows) == len(created)
        latest = [row["semver"] for row in rows if row["is_latest"]]
        assert latest == [min((row["semver"] for row in rows), key=semver_key)]
        assert invariant(stack.contracts_db_url) == []


# --- lifecycle races on one contract and across contracts (Task 2) ---------------------------


def versions_url(contract: str, ref: str | None = None) -> str:
    base = f"{CONTRACTS}/{contract}/versions"
    return base if ref is None else f"{base}/{ref}"


async def contract_with_versions(
    stack: Any, team: str, versions: tuple[str, ...] = ("1.1.0", "1.2.0")
) -> tuple[str, str]:
    """A contract with 1.0.0 as Latest plus a published Preview per entry of ``versions``."""
    odcs_id = new_odcs_id()
    contract, draft, etag = await stack.create_contract(team, doc(odcs_id))
    assert (await stack.publish(contract, draft, etag)).status_code == 201
    for version in versions:
        extra, extra_etag = await new_draft(
            stack, contract, odcs_id, version, source={"from_version": "1.0.0"}
        )
        published = await stack.publish(contract, extra, extra_etag)
        assert published.status_code == 201, published.text
    return contract, odcs_id


async def post_lifecycle(stack: Any, contract: str, ref: str, action: str) -> Any:
    return await stack.client.post(
        f"{versions_url(contract, ref)}/{action}", headers=bearer(stack.admin_token)
    )


def latest_rows(stack: Any, sql: Sql, contract: str) -> list[dict[str, Any]]:
    """The rows flagged ``is_latest`` for the contract, each with the contract's pointer."""
    return sql(
        stack.contracts_db_url,
        "SELECT v.id::text AS id, v.semver, v.state, c.latest_version_id::text AS pointer "
        "FROM contract_version v JOIN contract c ON c.id = v.contract_id "
        "WHERE v.contract_id = %s AND v.is_latest",
        contract,
    )


def linked_doc(odcs_id: str, urls: tuple[str, ...]) -> bytes:
    """A document linking to other contracts through ``authoritativeDefinitions[].url``."""
    text = doc(odcs_id).decode() + "authoritativeDefinitions:\n"
    text += "".join(f'  - type: businessDefinition\n    url: "{url}"\n' for url in urls)
    return text.encode()


async def test_concurrency_promote_promote_leaves_exactly_one_latest(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await contract_with_versions(stack, chain.team)
        targets = ("1.1.0", "1.2.0")
        for round_no in range(5):
            order = targets if round_no % 2 == 0 else targets[::-1]
            results = await asyncio.gather(
                *[post_lifecycle(stack, contract, ref, "promote") for ref in order * 4]
            )
            assert [r.status_code for r in results] == [200] * 8, [r.text for r in results]
            rows = latest_rows(stack, sql, contract)
            assert len(rows) == 1
            assert rows[0]["id"] == rows[0]["pointer"]
            assert rows[0]["semver"] in targets
            assert rows[0]["state"] == "published"
            assert invariant(stack.contracts_db_url) == []


async def test_concurrency_promote_unpublish_never_leaves_an_unpublished_latest(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await contract_with_versions(stack, chain.team, ("1.1.0",))
        outcomes: set[tuple[int, int]] = set()
        for _ in range(10):
            promote, unpublish = await asyncio.gather(
                post_lifecycle(stack, contract, "1.1.0", "promote"),
                post_lifecycle(stack, contract, "1.1.0", "unpublish"),
            )
            outcome = (promote.status_code, unpublish.status_code)
            assert outcome in {(200, 409), (409, 200)}, (promote.text, unpublish.text)
            if outcome == (200, 409):  # promoted first: the Latest cannot be unpublished
                assert problem_slug(unpublish) == "latest-version-protected"
            else:  # unpublished first: only a published version can be promoted
                assert problem_slug(promote) == "version-not-published"
            outcomes.add(outcome)
            rows = latest_rows(stack, sql, contract)
            assert len(rows) == 1
            assert rows[0]["state"] == "published"
            assert rows[0]["id"] == rows[0]["pointer"]
            assert invariant(stack.contracts_db_url) == []
            # Back to 1.0.0 Latest with 1.1.0 published, ready for the next round.
            assert (await post_lifecycle(stack, contract, "1.0.0", "promote")).status_code == 200
            assert (await post_lifecycle(stack, contract, "1.1.0", "republish")).status_code == 200
        assert outcomes <= {(200, 409), (409, 200)}


async def test_concurrency_referrer_delete_ends_consistently_without_deadlock(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        delete_won = 0
        for round_no in range(10):
            cross_org = round_no % 2 == 1
            target_odcs = new_odcs_id()
            target, target_draft, target_etag = await stack.create_contract(
                chain.team, doc(target_odcs)
            )
            assert (await stack.publish(target, target_draft, target_etag)).status_code == 201
            if cross_org:  # D-54: a link from another Organization blocks too
                team = (await stack.chain()).team
                link = f"https://dc.example{CONTRACTS}/{target}"
            else:
                team = await stack.team_in_org(chain.org, f"r{round_no}")
                link = f"{target_odcs}.yaml#/x"
            referrer, draft, etag = await stack.create_contract(
                team, linked_doc(new_odcs_id(), (link,))
            )

            published, deleted = await asyncio.gather(
                stack.publish(referrer, draft, etag),
                stack.client.delete(f"{CONTRACTS}/{target}", headers=bearer(stack.admin_token)),
            )
            for response in (published, deleted):
                assert response.status_code < 500, response.text
                assert "deadlock" not in response.text.lower()
            if published.status_code == 409:
                # IN-03: the delete committed after the gate read the link facts (published) and
                # before the publish lock; the re-check refuses, and the retry publishes against
                # the new facts (the target is now not_found).
                assert problem_slug(published) == "baseline-changed", published.text
                assert deleted.status_code == 204, deleted.text
                published = await stack.publish(referrer, draft, etag)
            assert published.status_code == 201, published.text
            assert deleted.status_code in {204, 409}, deleted.text
            links = sql(
                stack.contracts_db_url,
                "SELECT ref_kind, ref_value FROM contract_reference WHERE source_contract_id = %s",
                referrer,
            )
            assert len(links) == 1  # the reference is stored whichever side won
            gone = sql(
                stack.contracts_db_url, "SELECT deleted_at FROM contract WHERE id = %s", target
            )[0]["deleted_at"]
            if deleted.status_code == 409:  # the publish committed first
                assert problem_slug(deleted) == "contract-referenced"
                assert gone is None
            else:  # the delete committed first; T stays protected once it is restored
                delete_won += 1
                assert gone is not None
                restored = await stack.client.post(
                    f"{CONTRACTS}/{target}/restore", headers=bearer(stack.admin_token)
                )
                assert restored.status_code == 200, restored.text
                again = await stack.client.delete(
                    f"{CONTRACTS}/{target}", headers=bearer(stack.admin_token)
                )
                assert again.status_code == 409, again.text
                assert problem_slug(again) == "contract-referenced"
            assert invariant(stack.contracts_db_url) == []
        # Informational only: which side wins is the scheduler's choice, both are consistent.
        assert 0 <= delete_won <= 10


async def test_concurrency_draft_counter_gives_every_parallel_draft_its_own_number(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack(max_drafts_per_contract=50) as stack:
        chain = await stack.chain()
        contract, _odcs = await contract_with_versions(stack, chain.team, ())
        before = sql(
            stack.contracts_db_url,
            "SELECT version_counter FROM contract WHERE id = %s",
            contract,
        )[0]["version_counter"]

        results = await asyncio.gather(
            *[
                stack.client.post(
                    drafts_url(contract),
                    json={"from_version": "1.0.0"},
                    headers=bearer(stack.admin_token),
                )
                for _ in range(10)
            ]
        )

        assert [r.status_code for r in results] == [201] * 10, [r.text for r in results]
        after = sql(
            stack.contracts_db_url,
            "SELECT version_counter FROM contract WHERE id = %s",
            contract,
        )[0]["version_counter"]
        assert after == before + 10
        numbers = sql(
            stack.contracts_db_url,
            "SELECT version_no FROM contract_version WHERE contract_id = %s AND state = 'draft'",
            contract,
        )
        assert len({row["version_no"] for row in numbers}) == len(numbers) == 10
        assert invariant(stack.contracts_db_url) == []


async def test_concurrency_latest_reads_are_always_one_coherent_version(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, odcs_id = await contract_with_versions(stack, chain.team)
        known = {doc(odcs_id, version=v) for v in ("1.0.0", "1.1.0", "1.2.0")}
        known_semvers = {"1.0.0", "1.1.0", "1.2.0"}
        admin = bearer(stack.admin_token)

        async def promoter() -> list[Any]:
            sent = []
            for index in range(10):
                ref = "1.1.0" if index % 2 == 0 else "1.2.0"
                sent.append(await post_lifecycle(stack, contract, ref, "promote"))
            return sent

        async def reader(index: int) -> list[Any]:
            seen = []
            for _ in range(5):
                if index % 2 == 0:
                    seen.append(
                        await stack.client.get(versions_url(contract, "latest"), headers=admin)
                    )
                else:
                    seen.append(
                        await stack.client.get(
                            f"{versions_url(contract, 'latest')}/metadata", headers=admin
                        )
                    )
            return seen

        promoted, *read_batches = await asyncio.gather(promoter(), *[reader(i) for i in range(10)])

        assert [r.status_code for r in promoted] == [200] * 10
        for index, batch in enumerate(read_batches):
            for response in batch:
                assert response.status_code == 200, response.text
                if index % 2 == 0:
                    digest = hashlib.sha256(response.content).hexdigest()
                    assert response.headers["etag"] == f'"{digest}"'
                    assert response.content in known
                else:
                    body = response.json()
                    assert body["semver"] in known_semvers
                    assert body["status"] == "latest"
        assert invariant(stack.contracts_db_url) == []


async def test_concurrency_list_reads_show_exactly_one_latest(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, _odcs = await contract_with_versions(stack, chain.team)
        admin = bearer(stack.admin_token)

        async def promoter() -> list[Any]:
            sent = []
            for index in range(10):
                ref = "1.1.0" if index % 2 == 0 else "1.2.0"
                sent.append(await post_lifecycle(stack, contract, ref, "promote"))
            return sent

        async def reader() -> list[Any]:
            return [await stack.client.get(versions_url(contract), headers=admin) for _ in range(5)]

        promoted, *read_batches = await asyncio.gather(promoter(), *[reader() for _ in range(10)])

        assert [r.status_code for r in promoted] == [200] * 10
        for batch in read_batches:
            for response in batch:
                assert response.status_code == 200, response.text
                items = response.json()["items"]
                assert len(items) == 3
                assert [item["status"] for item in items].count("latest") == 1
        assert invariant(stack.contracts_db_url) == []


async def test_concurrency_mutual_refs_publish_without_deadlock(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        for _ in range(5):
            a_odcs, b_odcs = new_odcs_id(), new_odcs_id()
            a, a_draft, a_etag = await stack.create_contract(
                chain.team, linked_doc(a_odcs, (f"{b_odcs}.yaml#/x",))
            )
            b, b_draft, b_etag = await stack.create_contract(
                chain.team, linked_doc(b_odcs, (f"{a_odcs}.yaml#/x",))
            )

            first, second = await asyncio.gather(
                stack.publish(a, a_draft, a_etag), stack.publish(b, b_draft, b_etag)
            )

            assert (first.status_code, second.status_code) == (201, 201), (first.text, second.text)
            for contract in (a, b):
                links = sql(
                    stack.contracts_db_url,
                    "SELECT count(*) AS n FROM contract_reference WHERE source_contract_id = %s",
                    contract,
                )
                assert links[0]["n"] == 1
            assert invariant(stack.contracts_db_url) == []
