"""Live Entra group members: ``GET /entra/groups/{registration_id}/members`` (quick 261119-wp6).

The gate is the group detail's (``entra:read`` at the owner). Members come from the directory on
every call and are never stored. A member that is a Contractum user or service principal carries
the record id the UI links to, but only when the caller may open that record. Reuses the
``open_world`` fixture of the registration tests, with the in-memory fake directory.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from dc_auth.testing import ENTRA_TENANT_ID
from dc_identity.directory import DirectoryMember


def _load_registration_helpers() -> Any:
    name = "dc_identity_tests_entra_registrations"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).with_name("test_entra_registrations.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_registrations = _load_registration_helpers()
open_world = _registrations.open_world  # the fixture, re-exported so pytest finds it here
OpenWorld = _registrations.OpenWorld
World = _registrations.World
auth = _registrations.auth
entra_users = _registrations.entra_users
jit_service_principal = _registrations.jit_service_principal
new_guid = _registrations.new_guid
principal = _registrations.principal
register_sp = _registrations.register_sp
register_user = _registrations.register_user
slug_of = _registrations.slug_of

pytestmark = pytest.mark.integration


def members_url(registration_id: str) -> str:
    return f"/api/v1/identity/entra/groups/{registration_id}/members"


def member(
    kind: str, oid: str, name: str, *, secondary: str | None = None, enabled: bool | None = True
) -> DirectoryMember:
    return DirectoryMember(
        object_id=oid,
        display_name=name,
        secondary=secondary,
        kind=kind,  # type: ignore[arg-type]
        account_enabled=enabled if kind != "group" else None,
    )


async def registered_group(
    w: World, owner: str, token: str, name: str = "Data Eng"
) -> tuple[str, str]:
    """(registration id, object id) of a group the fake directory knows, registered at ``owner``."""
    oid = w.group(name)
    resp = await w.register_group(token, owner, oid)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], oid


async def test_a_global_admin_sees_members_and_the_records_they_can_open(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        reg, oid = await registered_group(w, w.root, w.admin)
        alice, bob, nested, sp = new_guid(), new_guid(), new_guid(), new_guid()
        w.directory.add_principal("user", alice, display_name="Alice")
        assert (await register_user(w, w.admin, alice)).status_code == 201
        assert (
            await register_sp(w, w.admin, w.root, await _directory_sp(w, sp, "Pipeline"))
        ).status_code == 201
        w.directory.set_group_members(
            oid,
            [
                member("user", alice, "Alice", secondary="alice@x.test"),
                member("user", bob, "Bob", secondary="bob@x.test", enabled=False),
                member("group", nested, "Nested team", secondary="nested@x.test"),
                member("service_principal", sp, "Pipeline", secondary="app-id-1"),
            ],
        )
        events = len(w.events("%"))
        resp = await w.client.get(members_url(reg), headers=auth(w.admin))
        assert resp.status_code == 200, resp.text
        assert len(w.events("%")) == events  # reads are not audited
        body = resp.json()
        assert body["truncated"] is False
        assert body["total"] == 4
        by_id = {i["object_id"]: i for i in body["items"]}
        [alice_row] = [r for r in entra_users(w) if r["object_id"] == alice]
        assert by_id[alice] == {
            "object_id": alice,
            "display_name": "Alice",
            "secondary": "alice@x.test",
            "kind": "user",
            "account_enabled": True,
            "registered": True,
            "principal_id": principal(alice),
            "record_id": str(alice_row["id"]),
        }
        assert by_id[bob]["registered"] is False
        assert by_id[bob]["record_id"] is None
        assert by_id[bob]["principal_id"] is None
        assert by_id[bob]["account_enabled"] is False
        assert by_id[nested]["kind"] == "group"
        assert by_id[nested]["account_enabled"] is None
        assert by_id[nested]["registered"] is False
        [sp_row] = [r for r in w.rows("service_principal") if r["object_id"] == sp]
        assert by_id[sp]["registered"] is True
        assert by_id[sp]["record_id"] == str(sp_row["id"])


async def _directory_sp(w: World, oid: str, name: str) -> str:
    w.directory.add_principal("service_principal", oid, display_name=name)
    return oid


async def test_an_org_admin_reads_its_group_and_only_opens_records_it_may_read(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        reg, oid = await registered_group(w, w.org_a, w.oa)
        alice = new_guid()
        own_sp, root_sp = new_guid(), new_guid()
        w.directory.add_principal("user", alice, display_name="Alice")
        assert (await register_user(w, w.admin, alice)).status_code == 201
        w.directory.add_principal("service_principal", own_sp, display_name="Own")
        w.directory.add_principal("service_principal", root_sp, display_name="Rooted")
        assert (await register_sp(w, w.oa, w.org_a, own_sp)).status_code == 201
        assert (await register_sp(w, w.admin, w.root, root_sp)).status_code == 201
        w.directory.set_group_members(
            oid,
            [
                member("user", alice, "Alice"),
                member("service_principal", own_sp, "Own"),
                member("service_principal", root_sp, "Rooted"),
            ],
        )
        resp = await w.client.get(members_url(reg), headers=auth(w.oa))
        assert resp.status_code == 200, resp.text
        by_id = {i["object_id"]: i for i in resp.json()["items"]}
        # An Organization admin holds no user:read at ROOT: the user stays listed, not linked.
        assert by_id[alice]["registered"] is False
        assert by_id[alice]["record_id"] is None
        assert by_id[own_sp]["registered"] is True
        assert by_id[root_sp]["registered"] is False  # owned by ROOT: not readable for org A


async def test_a_caller_who_cannot_read_the_group_gets_404_and_no_directory_call(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        reg, _oid = await registered_group(w, w.org_a, w.oa)
        editor = await w.local_token("te-members", "team_editor", w.team_a)
        nobody = await w.local_token("nobody-members", None, None)
        calls_before = len(w.directory.calls)
        for token in (w.ob, editor, nobody):
            resp = await w.client.get(members_url(reg), headers=auth(token))
            assert resp.status_code in (403, 404), resp.text
        unauth = await w.client.get(members_url(reg))
        assert unauth.status_code == 401
        assert len(w.directory.calls) == calls_before


async def test_an_unknown_registration_or_a_service_principal_id_is_404(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        sp = await jit_service_principal(w, w.org_a, "Etl")
        [sp_row] = [r for r in w.rows("service_principal") if r["object_id"] == sp]
        for rid in (str(uuid.uuid4()), str(sp_row["id"])):
            resp = await w.client.get(members_url(rid), headers=auth(w.admin))
            assert resp.status_code == 404, resp.text
            assert slug_of(resp) == "not-found"


async def test_a_group_that_vanished_from_the_directory_is_404(open_world: OpenWorld) -> None:
    async with open_world() as w:
        reg, oid = await registered_group(w, w.root, w.admin)
        w.directory._groups.pop((ENTRA_TENANT_ID.lower(), oid))
        resp = await w.client.get(members_url(reg), headers=auth(w.admin))
        assert resp.status_code == 404, resp.text


async def test_a_directory_outage_is_503_and_nothing_is_stored(open_world: OpenWorld) -> None:
    async with open_world() as w:
        reg, _oid = await registered_group(w, w.root, w.admin)
        events = len(w.events("%"))
        w.directory.fail = True
        resp = await w.client.get(members_url(reg), headers=auth(w.admin))
        assert resp.status_code == 503, resp.text
        assert slug_of(resp) == "service-unavailable"
        assert len(w.events("%")) == events


async def test_a_truncated_listing_reports_truncated_and_the_total(open_world: OpenWorld) -> None:
    async with open_world() as w:
        reg, oid = await registered_group(w, w.root, w.admin)
        w.directory.set_group_members(
            oid, [member("user", new_guid(), f"User {i:03d}") for i in range(230)]
        )
        resp = await w.client.get(members_url(reg), headers=auth(w.admin))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["items"]) == 200
        assert body["truncated"] is True
        assert body["total"] == 230
