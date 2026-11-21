"""Read-only personal access tokens (AUTH-06, D-66).

A PAT created with ``read_only`` holds only the read permissions its owner holds, and nothing that
writes, wherever the permission is evaluated. The matrix iterates over ``PERMISSIONS`` so a future
permission is covered the moment it is registered (``PermissionSpec.access`` is required).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from dc_auth import roles
from dc_auth.roles import PERMISSIONS

pytestmark = pytest.mark.integration

IDENTITY = "/api/v1/identity"
PATS = f"{IDENTITY}/pats"
CHECK = "/internal/v1/authz/check"
ACCESSIBLE = "/internal/v1/authz/accessible-folders"

Sql = Callable[..., list[dict[str, Any]]]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _mint(client: Any, token: str, *, read_only: bool, name: str = "ro") -> dict[str, Any]:
    resp = await client.post(
        PATS,
        json={
            "name": name,
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
            "read_only": read_only,
        },
        headers=_bearer(token),
    )
    assert resp.status_code == 201, resp.text
    body: dict[str, Any] = resp.json()
    return body


@dataclass
class World:
    client: Any
    admin: str
    team: str
    org: str
    editor: Any
    editor_jwt: str
    editor_grant_id: str
    team_admin: Any
    team_admin_jwt: str

    async def check(self, token: str, permission: str, folder_id: str | None = None) -> Any:
        return await self.client.post(
            CHECK,
            json={"permission": permission, "folder_id": folder_id or self.team},
            headers=_bearer(token),
        )

    async def folders(self, token: str, permission: str) -> dict[str, Any]:
        resp = await self.client.post(
            ACCESSIBLE, json={"permission": permission}, headers=_bearer(token)
        )
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        return body


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
            org, _project, team = await folder_chain(client, admin, root, "ro")
            editor = await create_user(client, admin, "editor")
            granted = await grant(client, admin, team, editor.principal_id, "team_editor")
            assert granted.status_code == 201, granted.text
            team_admin = await create_user(client, admin, "teamadmin")
            made = await grant(client, admin, team, team_admin.principal_id, "team_admin")
            assert made.status_code == 201, made.text
            yield World(
                client=client,
                admin=admin,
                team=team,
                org=org,
                editor=editor,
                editor_jwt=await login(client, editor.username, editor.password),
                editor_grant_id=granted.json()["id"],
                team_admin=team_admin,
                team_admin_jwt=await login(client, team_admin.username, team_admin.password),
            )

    return _open


async def test_the_global_admin_creates_a_read_only_pat_and_it_shows_in_the_body_and_the_list(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        body = await _mint(w.client, w.admin, read_only=True)
        full = await _mint(w.client, w.admin, read_only=False, name="full")
        listing = await w.client.get(PATS, headers=_bearer(w.admin))
    assert body["read_only"] is True and full["read_only"] is False
    flags = {item["name"]: item["read_only"] for item in listing.json()["items"]}
    assert flags == {"ro": True, "full": False}


async def test_a_read_only_pat_is_allowed_every_read_and_no_write_of_a_global_admin(
    open_world: Callable[..., Any],
) -> None:
    seen: dict[str, tuple[str, list[str]]] = {}
    async with open_world() as w:
        token = (await _mint(w.client, w.admin, read_only=True))["token"]
        full = (await _mint(w.client, w.admin, read_only=False, name="full"))["token"]
        for name in PERMISSIONS:
            resp = await w.check(token, name)
            assert resp.status_code == 200, (name, resp.text)
            seen[name] = (resp.json()["outcome"], resp.json()["permissions"])
        # Non-vacuous: the same owner's full PAT is allowed the folder writes, so a refusal of the
        # read-only PAT is the narrowing and not a missing grant.
        for name in ("folder:update", "contract:create"):
            assert (await w.check(full, name)).json()["outcome"] == "allow", name
    assert set(seen) == set(PERMISSIONS)
    for name, spec in PERMISSIONS.items():
        outcome, permissions = seen[name]
        if getattr(spec, "access", None) == "read":
            assert outcome == "allow", name
        else:
            assert outcome != "allow", name
        assert not set(permissions) - getattr(roles, "READ_PERMISSIONS", frozenset()), name


async def test_a_denied_write_is_403_when_the_folder_is_visible_and_never_a_success(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        token = (await _mint(w.client, w.admin, read_only=True))["token"]
        update = await w.check(token, "folder:update")
        create = await w.check(token, "contract:create")
    assert update.json()["outcome"] == "forbidden"
    assert create.json()["outcome"] == "forbidden"


async def test_a_read_only_pat_has_no_writable_folders_and_all_readable_ones(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        token = (await _mint(w.client, w.admin, read_only=True))["token"]
        written = await w.folders(token, "contract:create")
        read = await w.folders(token, "folder:read")
    assert written["all"] is False and written["folder_ids"] == []
    assert read["all"] is True


async def test_a_team_editors_read_only_pat_reads_the_folder_and_cannot_write(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        ro = (await _mint(w.client, w.team_admin_jwt, read_only=True))["token"]
        full = (await _mint(w.client, w.team_admin_jwt, read_only=False, name="full"))["token"]
        read = await w.client.get(f"{IDENTITY}/folders/{w.team}", headers=_bearer(ro))
        denied = await w.client.patch(
            f"{IDENTITY}/folders/{w.team}", json={"display_name": "x"}, headers=_bearer(ro)
        )
        allowed = await w.client.patch(
            f"{IDENTITY}/folders/{w.team}", json={"display_name": "renamed"}, headers=_bearer(full)
        )
        editor_ro = (await _mint(w.client, w.editor_jwt, read_only=True))["token"]
        editor_read = await w.client.get(f"{IDENTITY}/folders/{w.team}", headers=_bearer(editor_ro))
        editor_write = await w.client.patch(
            f"{IDENTITY}/folders/{w.team}", json={"display_name": "y"}, headers=_bearer(editor_ro)
        )
    assert read.status_code == 200, read.text
    assert denied.status_code == 403, denied.text
    assert allowed.status_code == 200, allowed.text
    assert editor_read.status_code == 200, editor_read.text
    assert editor_write.status_code in (403, 404), editor_write.text


async def test_a_read_only_pat_loses_a_read_its_owner_loses_on_the_next_request(
    open_world: Callable[..., Any],
) -> None:
    async with open_world() as w:
        token = (await _mint(w.client, w.editor_jwt, read_only=True))["token"]
        before = (await w.check(token, "folder:read")).json()["outcome"]
        revoked = await w.client.delete(
            f"{IDENTITY}/grants/{w.editor_grant_id}", headers=_bearer(w.admin)
        )
        after = (await w.check(token, "folder:read")).json()["outcome"]
    assert before == "allow"
    assert revoked.status_code == 204, revoked.text
    assert after != "allow"
