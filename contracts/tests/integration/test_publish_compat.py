"""The semver gate through contracts and the real validation app (VAL-02, VAL-03, D-143..D-148).

A breaking change against the highest published version needs a major bump above the highest
stable major; there is no override. The baseline and the floor travel in every run, are re-resolved
under the contract lock (409 ``baseline-changed``) and also reach save-mode runs.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

MakeStack = Callable[..., AbstractAsyncContextManager[Any]]
Sql = Callable[..., list[dict[str, Any]]]
Invariant = Callable[[str], list[dict[str, Any]]]

CONTRACTS = "/api/v1/contracts/contracts"
# The body of the most recent draft save made by ``new_draft`` (its embedded validation report).
LAST_SAVE: dict[str, Any] = {}


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
    return f"urn:dc:compat:{uuid.uuid4().hex}"


def published_events(stack: Any, sql: Sql) -> int:
    rows = sql(
        stack.contracts_db_url,
        "SELECT count(*) AS n FROM outbox WHERE action = 'contract.version_published'",
    )
    return int(rows[0]["n"])


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
    LAST_SAVE.clear()
    LAST_SAVE.update(saved.json())
    return str(draft["id"]), str(saved.headers["etag"])


async def publish_first(
    stack: Any, team: str, odcs_id: str, names: tuple[str, ...], version: str = "1.0.0"
) -> str:
    contract, draft, etag = await stack.create_contract(team, doc(odcs_id, *names, version=version))
    assert (await stack.publish(contract, draft, etag)).status_code == 201
    return contract


async def publish_next(
    stack: Any,
    contract: str,
    odcs_id: str,
    version: str,
    names: tuple[str, ...],
    *,
    base: str,
) -> Any:
    draft, etag = await new_draft(stack, contract, odcs_id, version, names, base=base)
    return await stack.publish(contract, draft, etag)


def last_publish_request(stack: Any) -> dict[str, Any]:
    publishes = [
        json.loads(r.content)
        for r in stack.validation_link.requests
        if json.loads(r.content).get("mode") == "publish"
    ]
    return publishes[-1]


def stack_reports(stack: Any, sql: Sql) -> list[dict[str, Any]]:
    rows = sql(stack.validation_db_url, "SELECT report FROM validation_run ORDER BY created_at")
    return [row["report"] for row in rows]


# -- tracer ---------------------------------------------------------------------------------------


async def test_removing_a_column_as_a_minor_is_refused_and_the_same_content_as_a_major_publishes(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_id()
        contract = await publish_first(stack, chain.team, odcs_id, ("id", "amount"))
        events_before = published_events(stack, sql)

        draft, etag = await new_draft(stack, contract, odcs_id, "1.1.0", ("id",), base="1.0.0")
        refused = await stack.publish(contract, draft, etag)

        assert refused.status_code == 422, refused.text
        problem = refused.json()
        assert problem["type"] == "urn:dc:problem:contract-validation-failed"
        report = problem["report"]
        assert report["summary"]["publishable"] is False
        assert [f for f in report["findings"] if f["rule_key"] == "semver-breaking-change"]
        assert report["compatibility"]["required_bump"] == "major"
        assert published_events(stack, sql) == events_before

        # The same content, declared as the next major, publishes (a major bump is the way out).
        major_draft, major_etag = await new_draft(
            stack, contract, odcs_id, "2.0.0", ("id",), base="1.0.0"
        )
        accepted = await stack.publish(contract, major_draft, major_etag)
        assert accepted.status_code == 201, accepted.text
        sent = last_publish_request(stack)
        assert sent["baseline"]["semver"] == "1.0.0"
        assert sent["stable_major_floor"] == 1
        assert base64.b64decode(sent["baseline"]["document_b64"]) == doc(
            odcs_id, "id", "amount", version="1.0.0"
        )
        assert invariant(stack.contracts_db_url) == []


async def test_a_first_publish_sends_no_baseline_and_the_report_says_no_baseline(
    make_stack: MakeStack, sql: Sql
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        contract, draft, etag = await stack.create_contract(
            chain.team, doc(new_id(), "id", "amount")
        )
        stack.validation_link.requests.clear()

        published = await stack.publish(contract, draft, etag)

        assert published.status_code == 201, published.text
        sent = last_publish_request(stack)
        assert sent["baseline"] is None
        assert sent["stable_major_floor"] is None
        stored = stack_reports(stack, sql)
        assert stored[-1]["compatibility"]["status"] == "no-baseline"


# -- the baseline-changed race --------------------------------------------------------------------


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


async def test_unpublishing_the_baseline_between_the_gate_and_the_lock_is_409_and_a_retry_works(
    make_stack: MakeStack, sql: Sql, invariant: Invariant
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_id()
        contract = await publish_first(stack, chain.team, odcs_id, ("id", "amount"))
        preview = await publish_next(
            stack, contract, odcs_id, "1.1.0", ("id", "amount"), base="1.0.0"
        )
        assert preview.status_code == 201, preview.text
        draft, etag = await new_draft(
            stack, contract, odcs_id, "1.2.0", ("id", "amount"), base="1.1.0"
        )
        events_before = published_events(stack, sql)

        async def unpublish_baseline() -> None:
            response = await stack.client.post(
                f"{CONTRACTS}/{contract}/versions/1.1.0/unpublish",
                headers=bearer(stack.admin_token),
            )
            assert response.status_code == 200, response.text

        real_gate = stack.contracts_app.state.publish_gate
        stack.contracts_app.state.publish_gate = RacingGate(real_gate, unpublish_baseline)
        try:
            stack.validation_link.requests.clear()
            raced = await stack.publish(contract, draft, etag)
            assert raced.status_code == 409, raced.text
            assert raced.json()["type"] == "urn:dc:problem:baseline-changed"
            assert last_publish_request(stack)["baseline"]["semver"] == "1.1.0"
            row = sql(
                stack.contracts_db_url,
                "SELECT state, validation_run_id FROM contract_version WHERE id = %s",
                draft,
            )[0]
            assert row == {"state": "draft", "validation_run_id": None}
            assert published_events(stack, sql) == events_before

            retried = await stack.publish(contract, draft, etag)
        finally:
            stack.contracts_app.state.publish_gate = real_gate
        assert retried.status_code == 201, retried.text
        assert last_publish_request(stack)["baseline"]["semver"] == "1.0.0"
        assert invariant(stack.contracts_db_url) == []


# -- prerelease lines and 0.x ---------------------------------------------------------------------


async def test_a_new_majors_prerelease_line_may_break_but_a_stable_patch_may_not(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_id()
        contract = await publish_first(stack, chain.team, odcs_id, ("id", "amount", "note"))
        base = "1.0.0"
        for version, names in (
            ("2.0.0-rc.1", ("id", "amount", "note")),
            ("2.0.0-rc.2", ("id", "amount")),  # removes a column, still above the stable major 1
            ("2.0.0", ("id", "amount")),
        ):
            response = await publish_next(stack, contract, odcs_id, version, names, base=base)
            assert response.status_code == 201, (version, response.text)
            base = version

        refused = await publish_next(stack, contract, odcs_id, "2.0.1", ("id",), base="2.0.0")

        assert refused.status_code == 422, refused.text
        assert refused.json()["report"]["compatibility"]["required_bump"] == "major"


async def test_zero_major_versions_are_literal_a_breaking_minor_is_refused_and_one_zero_passes(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_id()
        contract = await publish_first(
            stack, chain.team, odcs_id, ("id", "amount"), version="0.3.0"
        )

        refused = await publish_next(stack, contract, odcs_id, "0.4.0", ("id",), base="0.3.0")
        assert refused.status_code == 422, refused.text

        accepted = await publish_next(stack, contract, odcs_id, "1.0.0", ("id",), base="0.3.0")
        assert accepted.status_code == 201, accepted.text


# -- save-mode advice -----------------------------------------------------------------------------


async def test_a_saved_draft_and_the_draft_validate_route_already_say_the_publish_needs_a_major(
    make_stack: MakeStack,
) -> None:
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_id()
        contract = await publish_first(stack, chain.team, odcs_id, ("id", "amount"))

        draft, _etag = await new_draft(stack, contract, odcs_id, "1.1.0", ("id",), base="1.0.0")

        embedded = LAST_SAVE["validation"]
        assert embedded["compatibility"]["required_bump"] == "major"
        assert embedded["compatibility"]["bump_ok"] is False
        assert stack.validation_link.requests
        last = json.loads(stack.validation_link.requests[-1].content)
        assert last["mode"] == "save"
        assert last["stable_major_floor"] == 1

        validated = await stack.client.post(
            f"{CONTRACTS}/{contract}/drafts/{draft}/validate", headers=bearer(stack.admin_token)
        )
        assert validated.status_code == 200, validated.text
        assert validated.json()["report"]["compatibility"]["required_bump"] == "major"


# -- CR-02: a long published version never blocks the next run ------------------------------------

# A CI-stamped version: 66 characters of strict SemVer (build metadata has no length limit).
LONG_BUILD = "1.0.0+build.20261113.sha.0123456789abcdef0123456789abcdef012345678"


async def test_a_long_build_metadata_baseline_does_not_block_the_next_save_or_publish(
    make_stack: MakeStack,
) -> None:
    assert len(LONG_BUILD) == 66
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_id()
        contract = await publish_first(stack, chain.team, odcs_id, ("id",), version=LONG_BUILD)

        response = await publish_next(
            stack, contract, odcs_id, "1.1.0", ("id", "name"), base=LONG_BUILD
        )

        assert response.status_code == 201, response.text
        # The save embedded a real report, not {"status": "unavailable"}.
        assert LAST_SAVE["validation"]["status"] != "unavailable", LAST_SAVE["validation"]
        assert last_publish_request(stack)["baseline"]["semver"] == LONG_BUILD


async def test_a_baseline_version_above_the_wire_cap_is_sent_without_its_semver(
    make_stack: MakeStack,
) -> None:
    huge = "1.0.1+" + "b" * 2100
    async with make_stack() as stack:
        chain = await stack.chain()
        odcs_id = new_id()
        contract = await publish_first(stack, chain.team, odcs_id, ("id",))
        # A draft can only start from a version of at most 300 characters, so the huge version is
        # published second (from 1.0.0) and becomes the baseline of the third publish.
        second = await publish_next(stack, contract, odcs_id, huge, ("id",), base="1.0.0")
        assert second.status_code == 201, second.text

        response = await publish_next(
            stack, contract, odcs_id, "1.1.0", ("id", "name"), base="1.0.0"
        )

        assert response.status_code == 201, response.text
        sent = last_publish_request(stack)["baseline"]
        assert sent["semver"] is None and sent["document_b64"]
