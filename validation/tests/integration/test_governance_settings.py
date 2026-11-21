"""The stored governance grace period reaches validation's two runtime call sites (quick
261120-0ye, D-34): setting a rule set mandatory and publishing a version of a mandatory set.

Identity runs in the same process (``make_stack``), so the stored value travels the real route:
``PUT /settings/governance`` -> ``GET /internal/v1/settings/runtime`` with the caller's bearer.
Migrations keep reading the environment value and are untouched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from .test_mandatory import (
    auth,
    enforce_at,
    mandate,
    new_set,
    publish_next,
    setup,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

SETTINGS = "/api/v1/identity/settings"


async def store_warn_days(stack: Any, days: int) -> None:
    headers = auth(stack.admin_token)
    listing = await stack.identity.get(SETTINGS, headers=headers)
    assert listing.status_code == 200, listing.text
    etag = next(s["etag"] for s in listing.json()["sections"] if s["section"] == "governance")
    saved = await stack.identity.put(
        f"{SETTINGS}/governance",
        content=json.dumps({"mandatory_warn_days": days}),
        headers={**headers, "If-Match": etag, "Content-Type": "application/json"},
    )
    assert saved.status_code == 200, saved.text


def days_ahead(value: datetime | None) -> float:
    assert value is not None
    return (value - datetime.now(UTC)) / timedelta(days=1)


async def test_tracer_a_stored_grace_period_stamps_a_new_version_of_a_mandatory_set(
    make_stack: Any,
) -> None:
    async with make_stack() as stack:
        await store_warn_days(stack, 3)
        chain, pam, _ted, _ = await setup(stack)
        rule_set_id = await new_set(stack, chain.org, pam, 1)
        assert (await mandate(stack, rule_set_id, pam)).status_code == 200
        # Setting mandatory used the stored value (the env default is 14).
        assert 2.9 < days_ahead(enforce_at(stack, rule_set_id)[1]) <= 3.0

        await publish_next(stack, rule_set_id, pam, 2)
        assert 2.9 < days_ahead(enforce_at(stack, rule_set_id)[2]) <= 3.0


async def test_a_stored_zero_enforces_at_once_and_beats_the_env_value(make_stack: Any) -> None:
    async with make_stack(mandatory_warn_days=30) as stack:
        await store_warn_days(stack, 0)
        chain, pam, _ted, _ = await setup(stack)
        rule_set_id = await new_set(stack, chain.org, pam, 1)
        assert (await mandate(stack, rule_set_id, pam)).status_code == 200
        assert days_ahead(enforce_at(stack, rule_set_id)[1]) <= 0.001


async def test_with_nothing_stored_the_env_value_applies(make_stack: Any) -> None:
    async with make_stack(mandatory_warn_days=5) as stack:
        chain, pam, _ted, _ = await setup(stack)
        rule_set_id = await new_set(stack, chain.org, pam, 1)
        assert (await mandate(stack, rule_set_id, pam)).status_code == 200
        assert 4.9 < days_ahead(enforce_at(stack, rule_set_id)[1]) <= 5.0
