"""How a rule-impact page is shaped from validation's answer (RULE-10, RULE-11, D-158 .. D-161).

The database read (``load_page``) is replaced by a fixed page, so these tests pin what the page
functions do with validation's batch answer: a malformed or mismatched answer is 503 and never a
partial page, a blocking error without a failing rule counts as ``would_error``, a contract outside
the caller's metadata scope is counted as hidden and never named, and an empty scope never reaches
the database or validation at all.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from dc_auth.authorizer import FolderScope
from dc_contracts import impact
from dc_contracts.impact import Candidate, DryRunRequest, Page, PageLimits
from dc_contracts.settings import ContractsSettings
from dc_core.problems import ProblemError

pytestmark = pytest.mark.unit

BEARER = "caller-token"  # noqa: S105 - a test token, not a secret
ROOT, ORG, TEAM, OTHER_TEAM = (uuid.uuid4() for _ in range(4))
RULE_SET_ID = uuid.uuid4()
COUNTS_ERROR_ONLY = {"fail": 0, "error": 1, "warn": 0}
COUNTS_FAIL = {"fail": 1, "error": 0, "warn": 0}
EVERYWHERE = FolderScope(all=True)
NOWHERE = FolderScope(all=False)


def candidate(folder: uuid.UUID) -> Candidate:
    return Candidate(
        contract_id=uuid.uuid4(),
        folder_id=folder,
        ancestor_ids=(ROOT, ORG, folder),
        odcs_id=f"urn:dc:{uuid.uuid4().hex}",
        name="Orders",
        semver="1.0.0",
        media_type="application/yaml",
        version_id=uuid.uuid4(),
        size_bytes=3,
        pinned_rule_sets=({"rule_set_id": str(RULE_SET_ID), "version": 1, "mode": "warn"},),
    )


def page_of(*candidates: Candidate) -> Page:
    return Page(
        candidates=list(candidates),
        documents={c.version_id: b"doc" for c in candidates},
        next_cursor=None,
        total_in_scope=len(candidates),
    )


class FakeAuthorizer:
    """Answers ``accessible_folders`` from a fixed scope per permission (everything by default)."""

    def __init__(self, scopes: dict[str, FolderScope] | None = None) -> None:
        self.scopes = scopes or {}
        self.asked: list[str] = []

    async def accessible_folders(self, principal: Any, permission: str) -> FolderScope:
        self.asked.append(permission)
        return self.scopes.get(permission, EVERYWHERE)


class FakeValidation:
    """Returns a fixed batch answer and records which contracts were sent."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.sent: list[list[dict[str, Any]]] = []

    async def dry_run(
        self, rule_set_id: uuid.UUID, version: Any, scope: uuid.UUID, items: Any, bearer: str
    ) -> Any:
        self.sent.append(items)
        return self.answer

    async def drift(self, folder_id: uuid.UUID, items: Any, bearer: str) -> Any:
        self.sent.append(items)
        return self.answer


@pytest.fixture
def serve_page(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Make ``load_page`` answer ``page`` without a database."""

    def install(page: Page) -> None:
        async def fixed(session_factory: Any, where: Any, after: Any, limits: PageLimits) -> Page:
            assert where is not None
            return page

        monkeypatch.setattr(impact, "load_page", fixed)

    return install


def dry_run_item(c: Candidate, counts: dict[str, int], *, would_block: bool) -> dict[str, Any]:
    return {
        "contract_id": str(c.contract_id),
        "status": "evaluated",
        "would_block": would_block,
        "counts": counts,
        "failing_rules": [],
    }


def drift_item(c: Candidate, status: str) -> dict[str, Any]:
    return {
        "contract_id": str(c.contract_id),
        "status": status,
        "counts": COUNTS_FAIL,
        "failing_rules": [],
        "current_versions": [],
    }


async def dry_run(validation: FakeValidation, authorizer: FakeAuthorizer | None = None) -> Any:
    return await impact.dry_run_page(
        session_factory=None,  # type: ignore[arg-type]
        settings=ContractsSettings(),
        client=validation,  # type: ignore[arg-type]
        authorizer=authorizer or FakeAuthorizer(),  # type: ignore[arg-type]
        principal=None,  # type: ignore[arg-type]
        bearer=BEARER,
        req=DryRunRequest(
            rule_set_id=RULE_SET_ID,
            version="draft",
            rule_set_folder_id=ORG,
            rule_set_ancestor_ids=(ROOT, ORG),
            folder_id=None,
            cursor=None,
        ),
    )


async def drift(validation: FakeValidation, authorizer: FakeAuthorizer | None = None) -> Any:
    return await impact.drift_page(
        session_factory=None,  # type: ignore[arg-type]
        settings=ContractsSettings(),
        client=validation,  # type: ignore[arg-type]
        authorizer=authorizer or FakeAuthorizer(),  # type: ignore[arg-type]
        principal=None,  # type: ignore[arg-type]
        bearer=BEARER,
        folder_id=ORG,
        cursor=None,
    )


async def test_an_empty_drift_scope_reads_nothing_and_asks_validation_nothing() -> None:
    validation = FakeValidation({"items": []})
    authorizer = FakeAuthorizer({"governance:drift-read": NOWHERE})

    out = await drift(validation, authorizer)

    assert out.items == []
    assert out.total_in_scope == 0
    assert out.next_cursor is None
    assert out.summary.model_dump() == {
        "drifted": 0,
        "at_risk": 0,
        "compliant": 0,
        "unparseable": 0,
        "hidden": 0,
    }
    assert validation.sent == []
    assert authorizer.asked == ["governance:drift-read"]


def test_an_empty_scope_has_no_candidate_filter() -> None:
    assert impact.candidate_where(NOWHERE, [ORG]) is None


@pytest.mark.parametrize("answer", [{"items": [{"contract_id": "not-a-uuid"}]}, {"no_items": True}])
async def test_an_unreadable_dry_run_answer_is_503(serve_page: Any, answer: Any) -> None:
    serve_page(page_of(candidate(TEAM)))

    with pytest.raises(ProblemError) as exc:
        await dry_run(FakeValidation(answer))

    assert (exc.value.status, exc.value.slug) == (503, "validation-unavailable")
    assert exc.value.detail == "Contract validation answered with an unreadable response."


async def test_an_unreadable_drift_answer_is_503(serve_page: Any) -> None:
    serve_page(page_of(candidate(TEAM)))

    with pytest.raises(ProblemError) as exc:
        await drift(FakeValidation({"items": [{"status": "drifted"}]}))

    assert (exc.value.status, exc.value.slug) == (503, "validation-unavailable")


async def test_an_answer_for_other_contracts_than_sent_is_503(serve_page: Any) -> None:
    sent = candidate(TEAM)
    serve_page(page_of(sent))
    stranger = candidate(TEAM)
    answer = {"items": [dry_run_item(stranger, COUNTS_FAIL, would_block=True)]}

    with pytest.raises(ProblemError) as exc:
        await dry_run(FakeValidation(answer))

    assert exc.value.status == 503
    assert exc.value.detail == (
        "Contract validation answered for different contracts than were sent."
    )


async def test_a_duplicated_answer_item_is_503(serve_page: Any) -> None:
    sent = candidate(TEAM)
    serve_page(page_of(sent))
    item = dry_run_item(sent, COUNTS_FAIL, would_block=True)

    with pytest.raises(ProblemError) as exc:
        await dry_run(FakeValidation({"items": [item, item]}))

    assert exc.value.status == 503


async def test_a_blocking_error_without_a_failing_rule_counts_as_would_error(
    serve_page: Any,
) -> None:
    erroring, failing, passing = candidate(TEAM), candidate(TEAM), candidate(TEAM)
    serve_page(page_of(erroring, failing, passing))
    answer = {
        "items": [
            dry_run_item(erroring, COUNTS_ERROR_ONLY, would_block=True),
            dry_run_item(failing, COUNTS_FAIL, would_block=True),
            dry_run_item(passing, {"fail": 0, "error": 0, "warn": 0}, would_block=False),
        ]
    }

    out = await dry_run(FakeValidation(answer))

    assert out.summary.model_dump(by_alias=True) == {
        "would_fail": 1,
        "would_error": 1,
        "pass": 1,
        "hidden": 0,
    }
    assert [item.contract_id for item in out.items] == [
        erroring.contract_id,
        failing.contract_id,
        passing.contract_id,
    ]


async def test_drift_counts_a_contract_outside_the_metadata_scope_as_hidden(
    serve_page: Any,
) -> None:
    visible, unseen = candidate(TEAM), candidate(OTHER_TEAM)
    serve_page(page_of(visible, unseen))
    answer = {"items": [drift_item(visible, "drifted"), drift_item(unseen, "at_risk")]}
    metadata = FolderScope(all=False, folder_ids=frozenset({TEAM}))
    authorizer = FakeAuthorizer({"contract:read-metadata": metadata})

    out = await drift(FakeValidation(answer), authorizer)

    assert [item.contract_id for item in out.items] == [visible.contract_id]
    assert out.summary.model_dump() == {
        "drifted": 1,
        "at_risk": 1,
        "compliant": 0,
        "unparseable": 0,
        "hidden": 1,
    }
    [pin] = out.items[0].pinned_rule_sets
    assert (pin.rule_set_id, pin.version, pin.mode) == (RULE_SET_ID, 1, "warn")
