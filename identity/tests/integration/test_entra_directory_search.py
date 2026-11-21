"""Entra directory search: ``GET /entra/directory/search`` (quick 261119-ofa).

The gate is never weaker than registration: users need a Global admin (``user:create``), groups and
service principals need ``entra:manage`` at a ROOT or Organization owner; both are interactive only.
Reuses the ``open_world`` fixture of the registration tests, with the in-memory fake directory.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from dc_auth.authorizer import AuthorizerUnavailable, Decision, Outcome
from dc_auth.testing import ENTRA_TENANT_ID


def _load_registration_helpers() -> Any:
    """The helpers and the ``open_world`` fixture of the registration tests (importlib mode)."""
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
jit_service_principal = _registrations.jit_service_principal
new_guid = _registrations.new_guid
register_sp = _registrations.register_sp
register_user = _registrations.register_user
slug_of = _registrations.slug_of

pytestmark = pytest.mark.integration

SEARCH = "/api/v1/identity/entra/directory/search"


async def search(
    w: World,
    token: str | None,
    kind: str,
    q: str,
    *,
    owner: str | None = None,
    tenant_id: str | None = None,
) -> Any:
    params: dict[str, str] = {"kind": kind, "q": q}
    if owner is not None:
        params["owner_folder_id"] = owner
    if tenant_id is not None:
        params["tenant_id"] = tenant_id
    headers = auth(token) if token is not None else {}
    return await w.client.get(SEARCH, params=params, headers=headers)


async def test_a_global_admin_finds_users_and_sees_who_is_already_registered(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        alice, alicia = new_guid(), new_guid()
        w.directory.add_principal(
            "user", alice, display_name="Alice A", user_principal_name="alice@x.test"
        )
        w.directory.add_principal("user", alicia, display_name="Alicia B", mail="alicia@x.test")
        w.directory.add_principal("service_principal", new_guid(), display_name="Alibaba")
        assert (await register_user(w, w.admin, alice)).status_code == 201
        resp = await search(w, w.admin, "user", "ali")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["truncated"] is False
        assert body["items"] == [
            {
                "object_id": alice,
                "tenant_id": ENTRA_TENANT_ID,
                "display_name": "Alice A",
                "secondary": "alice@x.test",
                "kind": "user",
                "already_registered": True,
            },
            {
                "object_id": alicia,
                "tenant_id": ENTRA_TENANT_ID,
                "display_name": "Alicia B",
                "secondary": "alicia@x.test",
                "kind": "user",
                "already_registered": False,
            },
        ]
        assert w.events("%search%") == []  # reads are not audited


async def test_a_pasted_object_id_finds_exactly_that_object(open_world: OpenWorld) -> None:
    async with open_world() as w:
        oid = new_guid()
        w.directory.add_principal("user", oid, display_name="Zed")
        w.directory.add_principal("user", new_guid(), display_name="Zoe")
        resp = await search(w, w.admin, "user", oid.upper())
        assert resp.status_code == 200, resp.text
        assert [i["object_id"] for i in resp.json()["items"]] == [oid]


async def test_an_org_admin_searches_groups_for_its_own_organization_only(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        registered = w.group("Data Eng")
        w.group("Data Ops")
        assert (await w.register_group(w.oa, w.org_a, registered)).status_code == 201
        resp = await search(w, w.oa, "group", "data", owner=w.org_a)
        assert resp.status_code == 200, resp.text
        items = {i["display_name"]: i for i in resp.json()["items"]}
        assert items["Data Eng"]["already_registered"] is True
        assert items["Data Ops"]["already_registered"] is False
        assert items["Data Ops"]["kind"] == "group"
        other_org = await search(w, w.oa, "group", "data", owner=w.org_b)
        at_root = await search(w, w.oa, "group", "data", owner=w.root)
        assert other_org.status_code == 404, other_org.text
        assert slug_of(other_org) == "not-found"
        assert at_root.status_code in (403, 404), at_root.text


async def test_a_service_principal_is_registered_only_for_another_owner_or_when_direct(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        jit = await jit_service_principal(w, w.org_a, "Etl")
        direct = new_guid()
        w.directory.add_principal("service_principal", direct, display_name="Etl direct")
        assert (await register_sp(w, w.oa, w.org_a, direct)).status_code == 201
        same_owner = await search(w, w.oa, "service_principal", "etl", owner=w.org_a)
        flags = {i["object_id"]: i["already_registered"] for i in same_owner.json()["items"]}
        assert flags == {jit: False, direct: True}  # a JIT row of the same owner can be registered
        as_admin_at_root = await search(w, w.admin, "service_principal", "etl", owner=w.root)
        flags = {i["object_id"]: i["already_registered"] for i in as_admin_at_root.json()["items"]}
        assert flags == {jit: True, direct: True}  # another owner: the register call is a 409


async def test_a_service_principal_search_matches_the_application_id(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        sp, app_id = new_guid(), new_guid()
        w.directory.add_principal("service_principal", sp, display_name="Pipeline", app_id=app_id)
        resp = await search(w, w.admin, "service_principal", app_id, owner=w.root)
        assert resp.status_code == 200, resp.text
        [item] = resp.json()["items"]
        assert item["object_id"] == sp
        assert item["secondary"] == app_id


async def test_an_org_admin_and_a_team_editor_may_not_search_users(open_world: OpenWorld) -> None:
    async with open_world() as w:
        w.directory.add_principal("user", new_guid(), display_name="Alice")
        editor = await w.local_token("te-2", "team_editor", w.team_a)
        for token in (w.oa, editor):
            resp = await search(w, token, "user", "ali")
            assert resp.status_code in (403, 404), resp.text
        assert w.directory.calls == []  # a refused caller never makes identity ask the directory


async def test_a_team_editor_never_searches_groups_or_service_principals(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        editor = await w.local_token("te-3", "team_editor", w.team_a)
        for kind in ("group", "service_principal"):
            for owner in (w.org_a, w.root, w.team_a):
                resp = await search(w, editor, kind, "data", owner=owner)
                assert resp.status_code in (403, 404), (kind, owner, resp.text)
        assert w.directory.calls == []


async def test_a_principal_without_grants_is_refused(open_world: OpenWorld) -> None:
    async with open_world() as w:
        nobody = await w.local_token("nobody-1", None, None)
        for kind, owner in (("user", None), ("group", w.org_a), ("service_principal", w.org_a)):
            resp = await search(w, nobody, kind, "data", owner=owner)
            assert resp.status_code in (403, 404), (kind, resp.text)
        assert w.directory.calls == []


async def test_a_pat_is_refused_as_not_interactive(open_world: OpenWorld, create_pat: Any) -> None:
    async with open_world() as w:
        pat = (await create_pat(w.client, w.admin))["token"]
        for kind, owner in (("user", None), ("group", w.root)):
            resp = await search(w, pat, kind, "data", owner=owner)
            assert resp.status_code == 403, resp.text
            assert slug_of(resp) == "interactive-session-required"
        assert w.directory.calls == []


async def test_no_token_is_401(open_world: OpenWorld) -> None:
    async with open_world() as w:
        resp = await search(w, None, "user", "ali")
        assert resp.status_code == 401, resp.text
        assert w.directory.calls == []


async def test_groups_and_service_principals_need_an_owner(open_world: OpenWorld) -> None:
    async with open_world() as w:
        for kind in ("group", "service_principal"):
            resp = await search(w, w.admin, kind, "data")
            assert resp.status_code == 422, resp.text
            assert slug_of(resp) == "validation-error"
        assert w.directory.calls == []


async def test_a_project_or_team_cannot_own_a_searched_registration(open_world: OpenWorld) -> None:
    async with open_world() as w:
        for owner in (w.project_a, w.team_a):
            resp = await search(w, w.admin, "group", "data", owner=owner)
            assert resp.status_code == 422, resp.text
            assert slug_of(resp) == "owner-not-allowed"
        assert w.directory.calls == []


async def test_a_query_with_fewer_than_two_searchable_characters_is_422_and_unasked(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        for q in ("a", '"a"', "\\\\", " a "):
            resp = await search(w, w.admin, "user", q)
            assert resp.status_code == 422, (q, resp.text)
            assert slug_of(resp) == "validation-error"
            assert q.strip() not in resp.text or len(q.strip()) < 2
        too_long = await search(w, w.admin, "user", "x" * 101)
        assert too_long.status_code == 422
        assert w.directory.calls == []


async def test_a_malformed_tenant_or_owner_is_422(open_world: OpenWorld) -> None:
    async with open_world() as w:
        bad_tenant = await search(w, w.admin, "user", "ali", tenant_id="NOT-A-GUID")
        unknown_tenant = await search(w, w.admin, "user", "ali", tenant_id=str(uuid.uuid4()))
        bad_owner = await search(w, w.admin, "group", "ali", owner="nope")
        assert [r.status_code for r in (bad_tenant, unknown_tenant, bad_owner)] == [422] * 3
        assert w.directory.calls == []


async def test_a_directory_outage_is_503_without_echoing_the_query(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        w.directory.fail = True
        resp = await search(w, w.admin, "user", "secretquery")
        assert resp.status_code == 503, resp.text
        assert slug_of(resp) == "service-unavailable"
        assert "secretquery" not in resp.text


async def test_without_an_entra_issuer_search_is_503(
    open_world: OpenWorld, asgi_client: Any
) -> None:
    from dc_identity.main import create_identity_app

    async with open_world() as w:
        local_only = w.env.auth_settings.model_copy(
            update={"issuers": [c for c in w.env.auth_settings.issuers if c.kind != "entra"]}
        )
        app = create_identity_app(w.env.settings, local_only, w.directory)
        async with asgi_client(app) as client:
            resp = await client.get(
                SEARCH, params={"kind": "user", "q": "ali"}, headers=auth(w.admin)
            )
        assert resp.status_code == 503, resp.text
        assert slug_of(resp) == "service-unavailable"
        assert w.directory.calls == []


async def test_more_than_twenty_matches_are_truncated_to_twenty(open_world: OpenWorld) -> None:
    async with open_world() as w:
        for i in range(23):
            w.directory.add_principal("user", new_guid(), display_name=f"Bulk {i:02d}")
        resp = await search(w, w.admin, "user", "bulk")
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["items"]) == 20
        assert resp.json()["truncated"] is True


class _UserCreateAnswer:
    """The real authorizer, except that the in-handler ``user:create`` check answers ``outcome``
    (or is unavailable when ``outcome`` is ``None``)."""

    def __init__(self, inner: Any, outcome: Outcome | None) -> None:
        self._inner = inner
        self._outcome = outcome

    async def check(self, principal: Any, permission: str, *args: Any, **kwargs: Any) -> Any:
        if permission != "user:create":
            return await self._inner.check(principal, permission, *args, **kwargs)
        if self._outcome is None:
            raise AuthorizerUnavailable("down")
        return Decision(outcome=self._outcome)

    async def accessible_folders(self, principal: Any, permission: str) -> Any:
        return await self._inner.accessible_folders(principal, permission)


async def test_the_user_search_maps_each_user_create_answer_and_never_asks_the_directory(
    open_world: OpenWorld,
) -> None:
    answers: dict[str, Any] = {}
    async with open_world() as w:
        w.directory.add_principal("user", new_guid(), display_name="Alice")
        state = w.env.app.state
        original = state.authorizer_factory
        try:
            for outcome in (Outcome.NOT_FOUND, Outcome.FORBIDDEN, None):
                state.authorizer_factory = lambda request, o=outcome: _UserCreateAnswer(
                    original(request), o
                )
                answers[str(outcome)] = await search(w, w.admin, "user", "ali")
        finally:
            state.authorizer_factory = original
        assert w.directory.calls == []
    assert {name: (r.status_code, slug_of(r)) for name, r in answers.items()} == {
        "not_found": (404, "not-found"),
        "forbidden": (403, "forbidden"),
        "None": (503, "service-unavailable"),
    }
