"""``rule_ctx``: the ctx binding of a run carries why it happens (quick 261119-rpi)."""

from __future__ import annotations

import uuid

import pytest
from dc_auth.authorizer import FolderFacts
from dc_validation.runs import rule_ctx

pytestmark = pytest.mark.unit

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")


def facts(organization_id: uuid.UUID | None) -> FolderFacts:
    return FolderFacts(
        id=uuid.uuid4(),
        kind="team",
        slug="ledger",
        path="root.acme.ledger",
        ancestor_ids=(),
        organization_id=organization_id,
        archived=False,
    )


@pytest.mark.parametrize("operation", ["save", "publish"])
def test_rule_ctx_has_the_three_folder_keys_and_the_operation(operation: str) -> None:
    got = rule_ctx(facts(ORG), operation)  # type: ignore[arg-type]

    assert got == {
        "folder_kind": "team",
        "folder_path": "root.acme.ledger",
        "organization_id": str(ORG),
        "operation": operation,
    }


def test_rule_ctx_organization_is_none_without_an_organization() -> None:
    assert rule_ctx(facts(None), "save")["organization_id"] is None
