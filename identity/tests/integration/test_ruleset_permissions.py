"""Phase 5 permissions through ``/internal/v1/authz/check`` (RULE-03, D-113, D-115, D-127).

identity's PDP is data-driven: the new ``ruleset:*`` and ``contract:validate`` permissions are
answered from ``dc_auth.roles`` with no identity code change. Each case grants real roles through
the grant API and asks identity the way a service would, with the caller's own token.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytestmark = pytest.mark.integration

PATS = "/api/v1/identity/pats"
CHECK = "/internal/v1/authz/check"
Sql = Callable[..., list[dict[str, Any]]]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class World:
    client: Any
    root: str
    org_a: str
    team_a1: str
    org_b: str
    pm_jwt: str
    editor_jwt: str
    reader_jwt: str
    consumer_jwt: str

    async def outcome(self, token: str, permission: str, folder_id: str) -> str:
        resp = await self.client.post(
            CHECK,
            json={"permission": permission, "folder_id": folder_id},
            headers=_bearer(token),
        )
        assert resp.status_code == 200, resp.text
        return str(resp.json()["outcome"])

    async def pat(self, jwt: str, *, read_only: bool) -> str:
        resp = await self.client.post(
            PATS,
            json={
                "name": "ruleset",
                "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                "read_only": read_only,
            },
            headers=_bearer(jwt),
        )
        assert resp.status_code == 201, resp.text
        token: str = resp.json()["token"]
        return token


@pytest.fixture
def open_world(
    make_identity: Callable[..., Any],
    asgi_client: Any,
    admin_token: Callable[[Any, Any], Awaitable[str]],
    folder_chain: Callable[..., Awaitable[tuple[str, str, str]]],
    create_user: Callable[..., Awaitable[Any]],
    login: Callable[..., Awaitable[str]],
    grant: Callable[..., Awaitable[Any]],
    sql: Sql,
) -> Callable[..., Any]:
    @asynccontextmanager
    async def _open() -> AsyncIterator[World]:
        env = make_identity()
        async with asgi_client(env.app) as client:
            admin = await admin_token(client, env)
            root = str(sql(env.db_url, "SELECT id FROM folder WHERE kind = 'root'")[0]["id"])
            org_a, _project_a, team_a1 = await folder_chain(client, admin, root, "a")
            org_b, _project_b, _team_b1 = await folder_chain(client, admin, root, "b")
            jwts: dict[str, str] = {}
            for name, role, folder in (
                ("pm", "policy_manager", org_a),
                ("editor", "team_editor", team_a1),
                ("reader", "team_reader", team_a1),
                ("consumer", "consumer", team_a1),
            ):
                user = await create_user(client, admin, f"{name}-user")
                granted = await grant(client, admin, folder, user.principal_id, role)
                assert granted.status_code == 201, granted.text
                jwts[name] = await login(client, user.username, user.password)
            yield World(
                client=client,
                root=root,
                org_a=org_a,
                team_a1=team_a1,
                org_b=org_b,
                pm_jwt=jwts["pm"],
                editor_jwt=jwts["editor"],
                reader_jwt=jwts["reader"],
                consumer_jwt=jwts["consumer"],
            )

    return _open


async def test_a_policy_manager_manages_rule_sets_at_their_folder_and_below_only(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        below = await w.outcome(w.pm_jwt, "ruleset:create", w.team_a1)
        at = await w.outcome(w.pm_jwt, "ruleset:create", w.org_a)
        parent = await w.outcome(w.pm_jwt, "ruleset:create", w.root)
        sibling = await w.outcome(w.pm_jwt, "ruleset:create", w.org_b)
        mandate = await w.outcome(w.pm_jwt, "ruleset:mandate", w.team_a1)
        mandate_at = await w.outcome(w.pm_jwt, "ruleset:mandate", w.org_a)
        mandate_parent = await w.outcome(w.pm_jwt, "ruleset:mandate", w.root)
        mandate_sibling = await w.outcome(w.pm_jwt, "ruleset:mandate", w.org_b)
        validate = await w.outcome(w.pm_jwt, "contract:validate", w.team_a1)
    assert (below, at, mandate, mandate_at, validate) == ("allow",) * 5
    # Grants inherit downward only: a lower level never edits or mandates a higher level's rules.
    assert parent != "allow" and sibling != "allow"
    assert mandate_parent != "allow" and mandate_sibling != "allow"


async def test_a_policy_manager_still_reads_no_contract_content(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        outcomes = {
            name: await w.outcome(w.pm_jwt, name, w.team_a1)
            for name in ("contract:read", "contract:update", "contract:publish")
        }
    assert all(outcome != "allow" for outcome in outcomes.values()), outcomes


async def test_a_team_editor_reads_published_rules_and_validates_but_cannot_author(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        create = await w.outcome(w.editor_jwt, "ruleset:create", w.team_a1)
        mandate = await w.outcome(w.editor_jwt, "ruleset:mandate", w.team_a1)
        validate = await w.outcome(w.editor_jwt, "contract:validate", w.team_a1)
        published = await w.outcome(w.editor_jwt, "ruleset:read-published", w.team_a1)
        drafts = await w.outcome(w.editor_jwt, "ruleset:read", w.team_a1)
    assert create != "allow" and mandate != "allow" and drafts != "allow"
    assert validate == "allow"
    assert published == "allow"


async def test_a_team_reader_validates_and_reads_published_rules(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        validate = await w.outcome(w.reader_jwt, "contract:validate", w.team_a1)
        published = await w.outcome(w.reader_jwt, "ruleset:read-published", w.team_a1)
        create = await w.outcome(w.reader_jwt, "ruleset:create", w.team_a1)
    assert (validate, published) == ("allow", "allow")
    assert create != "allow"


async def test_a_consumer_reads_published_rules_but_cannot_validate(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        published = await w.outcome(w.consumer_jwt, "ruleset:read-published", w.team_a1)
        validate = await w.outcome(w.consumer_jwt, "contract:validate", w.team_a1)
    assert published == "allow"
    assert validate != "allow"


async def test_a_read_only_pat_validates_because_validation_is_read_class(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        token = await w.pat(w.editor_jwt, read_only=True)
        validate = await w.outcome(token, "contract:validate", w.team_a1)
        published = await w.outcome(token, "ruleset:read-published", w.team_a1)
    assert (validate, published) == ("allow", "allow")


async def test_a_read_only_pat_of_a_policy_manager_cannot_change_rules(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        token = await w.pat(w.pm_jwt, read_only=True)
        outcomes = {
            name: await w.outcome(token, name, w.team_a1)
            for name in (
                "ruleset:create",
                "ruleset:update",
                "ruleset:publish",
                "ruleset:deprecate",
                "ruleset:mandate",
            )
        }
        read = await w.outcome(token, "ruleset:read", w.team_a1)
    assert all(outcome != "allow" for outcome in outcomes.values()), outcomes
    # Non-vacuous: the same token still reads, so the refusals are the narrowing.
    assert read == "allow"


async def test_a_full_pat_of_a_policy_manager_may_mandate_because_it_is_not_interactive_only(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        token = await w.pat(w.pm_jwt, read_only=False)
        mandate = await w.outcome(token, "ruleset:mandate", w.team_a1)
        create = await w.outcome(token, "ruleset:create", w.org_a)
    assert (mandate, create) == ("allow", "allow")
