"""Rule-impact pointers are content: only ``contract:read`` holders get them (CR-01, D-36).

A failing rule's JSON Pointers say where its target matched. With an author-controlled target
(``$..[?search(@, '^AKIA')]``) they map values of a document the caller may not read, so
``visible_rules`` empties them outside the caller's ``contract:read`` scope and keeps the verdict.
"""

from __future__ import annotations

import uuid

import pytest
from dc_auth.authorizer import FolderScope
from dc_contracts.impact import Candidate, ContractsImpactFailingRuleOut, visible_rules

pytestmark = pytest.mark.unit

ROOT, ORG, TEAM, OTHER = (uuid.uuid4() for _ in range(4))


def candidate(folder: uuid.UUID, *ancestors: uuid.UUID) -> Candidate:
    return Candidate(
        contract_id=uuid.uuid4(),
        folder_id=folder,
        ancestor_ids=(*ancestors, folder),
        odcs_id="urn:x",
        name="Orders",
        semver="1.0.0",
        media_type="application/yaml",
        version_id=uuid.uuid4(),
        size_bytes=10,
        pinned_rule_sets=(),
    )


def failing(pointers: list[str]) -> ContractsImpactFailingRuleOut:
    return ContractsImpactFailingRuleOut(
        rule_set_id=uuid.uuid4(),
        version_no=None,
        mode="enforce",
        rule_key="find-keys",
        severity="error",
        outcome="fail",
        message="Found.",
        error_code=None,
        blocking=True,
        pointers=pointers,
        pointers_total=len(pointers),
    )


def test_a_caller_with_contract_read_keeps_every_pointer() -> None:
    rules = [failing(["/schema/0/properties/3/description"])]
    scope = FolderScope(all=False, folder_ids=frozenset({TEAM}))

    [kept] = visible_rules(rules, scope, candidate(TEAM, ROOT, ORG))

    assert kept.pointers == ["/schema/0/properties/3/description"]
    assert kept.pointers_total == 1
    assert kept.pointers_withheld is False


@pytest.mark.parametrize(
    "scope",
    [
        FolderScope(all=False),
        FolderScope(all=False, folder_ids=frozenset({OTHER})),
        FolderScope(all=True, excluded_subtrees=frozenset({ORG})),
    ],
    ids=["empty", "another-folder", "excluded-subtree"],
)
def test_without_contract_read_the_verdict_stays_and_the_pointers_go(scope: FolderScope) -> None:
    rules = [failing(["/schema/0", "/schema/1"]), failing([])]

    shown = visible_rules(rules, scope, candidate(TEAM, ROOT, ORG))

    assert [r.rule_key for r in shown] == ["find-keys", "find-keys"]
    assert all(r.blocking and r.outcome == "fail" for r in shown)
    assert all(r.pointers == [] and r.pointers_total == 0 for r in shown)
    assert all(r.pointers_withheld for r in shown)
    # The originals are not mutated (validation's answer may be reused for the totals).
    assert rules[0].pointers == ["/schema/0", "/schema/1"]
