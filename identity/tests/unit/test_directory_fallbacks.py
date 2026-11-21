"""The unconfigured directory fails closed, and the fake directory's search keeps tenants apart."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from dc_auth.testing import ENTRA_TENANT_ID
from dc_identity.directory import DirectoryUnavailable, UnconfiguredDirectory
from dc_identity.directory.fake import FakeDirectoryClient

pytestmark = pytest.mark.unit

OTHER_TENANT = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
_OID = str(uuid.uuid4())

_CALLS: dict[str, Callable[[UnconfiguredDirectory], Awaitable[Any]]] = {
    "lookup": lambda d: d.lookup(ENTRA_TENANT_ID, "user", _OID),
    "group": lambda d: d.group(ENTRA_TENANT_ID, _OID),
    "search": lambda d: d.search(ENTRA_TENANT_ID, "group", "eng"),
    "group_members": lambda d: d.group_members(ENTRA_TENANT_ID, _OID),
}


@pytest.mark.parametrize("call", sorted(_CALLS))
async def test_every_directory_question_fails_closed_without_a_directory(call: str) -> None:
    with pytest.raises(DirectoryUnavailable, match="no directory is configured"):
        await _CALLS[call](UnconfiguredDirectory())


async def test_the_unconfigured_probes_answer_not_configured_and_no_expiries() -> None:
    directory = UnconfiguredDirectory()
    assert await directory.check_connection(ENTRA_TENANT_ID) == "not-configured"
    assert await directory.application_secret_expiries(ENTRA_TENANT_ID, "app") is None


async def test_a_group_search_returns_only_matching_groups_of_the_asked_tenant() -> None:
    fake = FakeDirectoryClient()
    wanted, unrelated, foreign = (str(uuid.uuid4()) for _ in range(3))
    fake.add_group(wanted, "Data Engineering")
    fake.add_group(unrelated, "Finance")
    fake.add_group(foreign, "Data Engineering", tenant_id=OTHER_TENANT)
    result = await fake.search(ENTRA_TENANT_ID, "group", "engineering")
    assert [(m.object_id, m.display_name, m.kind) for m in result.items] == [
        (wanted, "Data Engineering", "group")
    ]
    assert result.truncated is False
