"""Platform settings: store, audit, access, import and export, runtime route, About, Entra views.

Quick 261120-0yc. Reuses the ``open_world`` fixture of the registration tests (a Global admin,
two Organizations with Org admins, the in-memory fake directory).

Refusal statuses recorded here (the UI spec asserts the same): every refused caller of the ROOT
permissions ``settings:read`` and ``settings:write`` (Global reader, Org admin, Team editor, a
principal with no grant) gets **403**, never 404. A PAT of the Global admin may read
(``settings:read`` is read class) and gets 403 ``interactive-session-required`` on every write.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from dc_auth.testing import ENTRA_TENANT_ID

pytestmark = pytest.mark.integration


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


_reg = _load_registration_helpers()
open_world = _reg.open_world  # the fixture, re-exported so pytest finds it here
OpenWorld = _reg.OpenWorld
World = _reg.World
auth = _reg.auth
new_guid = _reg.new_guid
slug_of = _reg.slug_of
OTHER_TENANT = _reg.OTHER_TENANT

SETTINGS = "/api/v1/identity/settings"
RUNTIME = "/internal/v1/settings/runtime"
IDENTITY = "/api/v1/identity"


def events(w: Any, like: str = "settings.%") -> list[dict[str, Any]]:
    return w.sql(
        w.env.db_url,
        "SELECT action, aggregate_type, aggregate_id, before, after, scope_path "
        "FROM outbox WHERE action LIKE %s ORDER BY id",
        like,
    )


async def put(w: Any, token: str, section: str, body: dict[str, Any], etag: str | None) -> Any:
    headers = auth(token)
    if etag is not None:
        headers["If-Match"] = etag
    return await w.client.put(f"{SETTINGS}/{section}", json=body, headers=headers)


async def combined_etag(w: Any) -> str:
    resp = await w.client.get(SETTINGS, headers=auth(w.admin))
    assert resp.status_code == 200, resp.text
    return str(resp.json()["etag"])


def export_file(sections: dict[str, Any]) -> bytes:
    return json.dumps(
        {"format": "contractum-settings", "format_version": 1, "sections": sections}
    ).encode()


async def post_import(
    w: Any, token: str, body: bytes, *, dry_run: bool = True, etag: str | None = None
) -> Any:
    headers = {**auth(token), "Content-Type": "application/json"}
    if etag is not None:
        headers["If-Match"] = etag
    return await w.client.post(
        f"{SETTINGS}/import",
        params={"dry_run": str(dry_run).lower()},
        content=body,
        headers=headers,
    )


# --- the tracer slice ---------------------------------------------------------------------------


async def test_tracer_a_saved_default_tenant_is_stored_audited_served_and_used(
    open_world: OpenWorld, fake_issuer: Any
) -> None:
    second = fake_issuer.entra_issuer_config(tenant_id=OTHER_TENANT, namespace="entra-b")
    async with open_world(extra_issuers=[second]) as w:
        resp = await put(w, w.admin, "entra", {"default_tenant_id": OTHER_TENANT}, '"entra-r0"')
        assert resp.status_code == 200, resp.text
        assert resp.headers["etag"] == '"entra-r1"'
        body = resp.json()
        assert body["values"] == {"default_tenant_id": OTHER_TENANT}
        assert body["revision"] == 1
        rows = w.sql(w.env.db_url, "SELECT section, value, revision FROM platform_setting")
        assert rows == [
            {"section": "entra", "value": {"default_tenant_id": OTHER_TENANT}, "revision": 1}
        ]
        found = events(w)
        assert len(found) == 1
        assert found[0]["action"] == "settings.updated"
        assert found[0]["aggregate_type"] == "platform_settings"
        assert found[0]["aggregate_id"] == "entra"
        assert found[0]["before"] == {"entra": {}}
        assert found[0]["after"] == {"entra": {"default_tenant_id": OTHER_TENANT}}
        assert [str(i) for i in found[0]["scope_path"]] == [w.root]

        listed = await w.client.get(SETTINGS, headers=auth(w.admin))
        assert listed.status_code == 200
        sections = {s["section"]: s for s in listed.json()["sections"]}
        assert sections["entra"]["values"] == {"default_tenant_id": OTHER_TENANT}
        assert sections["entra"]["etag"] == '"entra-r1"'
        assert set(listed.json()["choices"]["entra_tenants"]) == {ENTRA_TENANT_ID, OTHER_TENANT}
        public = await w.client.get(f"{SETTINGS}/public", headers=auth(w.admin))
        assert public.status_code == 200
        assert public.json()["entra_default_tenant_id"] == OTHER_TENANT
        assert public.headers["cache-control"] == "private, max-age=30"

        oid = w.group("Tenant B group", tenant_id=OTHER_TENANT)
        created = await w.register_group(w.admin, w.root, oid)  # no tenant_id
        assert created.status_code == 201, created.text
        assert created.json()["tenant_id"] == OTHER_TENANT


# --- access -------------------------------------------------------------------------------------


async def test_only_a_global_admin_may_use_settings(open_world: OpenWorld) -> None:
    async with open_world() as w:
        reader = await w.local_token("settings-reader", "global_reader", w.root)
        org_admin = await w.local_token("settings-orgadmin", "org_admin", w.org_a)
        editor = await w.local_token("settings-editor", "team_editor", w.team_a)
        nobody = await w.local_token("settings-nobody", None, None)
        for token in (reader, org_admin, editor, nobody):
            for method, path in (
                ("GET", SETTINGS),
                ("GET", f"{SETTINGS}/export"),
                ("GET", f"{SETTINGS}/about"),
                ("GET", f"{SETTINGS}/entra/overview"),
            ):
                resp = await w.client.request(method, path, headers=auth(token))
                assert resp.status_code == 403, (path, resp.text)
            for path in (f"{SETTINGS}/entra/test", f"{SETTINGS}/import"):
                resp = await w.client.post(path, headers=auth(token))
                assert resp.status_code == 403, (path, resp.text)
            denied = await put(w, token, "platform", {}, '"platform-r0"')
            assert denied.status_code == 403
            # The public view is for every signed-in user.
            public = await w.client.get(f"{SETTINGS}/public", headers=auth(token))
            assert public.status_code == 200
        anonymous = await w.client.get(SETTINGS)
        assert anonymous.status_code == 401
        assert (await w.client.get(f"{SETTINGS}/public")).status_code == 401
        assert events(w) == []


async def test_a_pat_may_read_but_never_write_and_the_global_admin_reaches_every_route(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        for path in (
            SETTINGS,
            f"{SETTINGS}/public",
            f"{SETTINGS}/export",
            f"{SETTINGS}/entra/overview",
        ):
            resp = await w.client.get(path, headers=auth(w.admin))
            assert resp.status_code == 200, (path, resp.text)
        pat = await w.client.post(
            f"{IDENTITY}/pats",
            json={
                "name": "settings",
                "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
            },
            headers=auth(w.admin),
        )
        assert pat.status_code == 201, pat.text
        token = pat.json()["token"]
        assert (await w.client.get(SETTINGS, headers=auth(token))).status_code == 200
        refused = await put(w, token, "platform", {}, '"platform-r0"')
        assert refused.status_code == 403
        assert slug_of(refused) == "interactive-session-required"
        imported = await post_import(w, token, export_file({}), dry_run=False, etag='"all-x"')
        assert imported.status_code == 403
        assert slug_of(imported) == "interactive-session-required"
        assert events(w) == []


# --- writes -------------------------------------------------------------------------------------


async def test_if_match_is_required_and_checked_and_a_noop_writes_no_event(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        missing = await put(w, w.admin, "platform", {}, None)
        assert missing.status_code == 428
        stale = await put(w, w.admin, "platform", {}, '"platform-r7"')
        assert stale.status_code == 412
        unknown = await put(w, w.admin, "nope", {}, '"nope-r0"')
        assert unknown.status_code == 404
        assert slug_of(unknown) == "settings-section-not-found"
        banner = {"banner": {"text": "Maintenance tonight", "severity": "warning"}}
        first = await put(w, w.admin, "platform", banner, '"platform-r0"')
        assert first.status_code == 200, first.text
        assert len(events(w)) == 1
        again = await put(w, w.admin, "platform", banner, '"platform-r1"')
        assert again.status_code == 200
        assert again.json()["revision"] == 1
        assert len(events(w)) == 1  # the same values again: no event
        stale_again = await put(w, w.admin, "platform", banner, '"platform-r0"')
        assert stale_again.status_code == 412


async def test_validation_errors_carry_a_pointer_and_nothing_is_written(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        bad_field = await put(w, w.admin, "entra", {"surprise": 1}, '"entra-r0"')
        assert bad_field.status_code == 422
        assert bad_field.json()["errors"][0]["pointer"] == "/surprise"
        unknown_tenant = await put(
            w, w.admin, "entra", {"default_tenant_id": OTHER_TENANT}, '"entra-r0"'
        )
        assert unknown_tenant.status_code == 422
        assert unknown_tenant.json()["errors"][0]["pointer"] == "/default_tenant_id"
        bad_owner = await put(
            w, w.admin, "entra", {"default_owner_folder_id": w.team_a}, '"entra-r0"'
        )
        assert bad_owner.status_code == 422
        assert slug_of(bad_owner) == "owner-not-allowed"
        missing_owner = await put(
            w, w.admin, "entra", {"default_owner_folder_id": new_guid()}, '"entra-r0"'
        )
        assert missing_owner.status_code == 422
        assert slug_of(missing_owner) == "owner-not-allowed"
        newline = await put(
            w, w.admin, "platform", {"banner": {"text": "two\nlines"}}, '"platform-r0"'
        )
        assert newline.status_code == 422
        assert newline.json()["errors"][0]["pointer"] == "/banner/text"
        assert w.sql(w.env.db_url, "SELECT 1 FROM platform_setting") == []
        assert events(w) == []
        ok = await put(
            w,
            w.admin,
            "entra",
            {"default_tenant_id": ENTRA_TENANT_ID, "default_owner_folder_id": w.org_a},
            '"entra-r0"',
        )
        assert ok.status_code == 200, ok.text
        public = await w.client.get(f"{SETTINGS}/public", headers=auth(w.admin))
        assert public.json()["entra_default_owner_folder_id"] == w.org_a
        row = events(w)[0]
        assert "secret" not in json.dumps(row["after"]).lower()


async def test_a_stale_default_tenant_falls_back_to_the_single_tenant_rule(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        # Store a tenant that is no longer configured, as if DC_AUTH_ISSUERS had changed.
        w.sql(
            w.env.db_url,
            "INSERT INTO platform_setting (section, value, created_by, updated_by) "
            "VALUES ('entra', %s::jsonb, 't', 't')",
            json.dumps({"default_tenant_id": OTHER_TENANT}),
        )
        oid = w.group()
        created = await w.register_group(w.admin, w.root, oid)
        assert created.status_code == 201, created.text
        assert created.json()["tenant_id"] == ENTRA_TENANT_ID
        public = await w.client.get(f"{SETTINGS}/public", headers=auth(w.admin))
        assert public.json()["entra_default_tenant_id"] is None


async def test_the_public_view_shows_only_an_active_banner(open_world: OpenWorld) -> None:
    async with open_world() as w:
        now = datetime.now(UTC)

        async def banner_after(starts: datetime | None, ends: datetime | None, etag: str) -> Any:
            banner: dict[str, Any] = {"text": "Heads up"}
            if starts:
                banner["starts_at"] = starts.isoformat()
            if ends:
                banner["ends_at"] = ends.isoformat()
            saved = await put(w, w.admin, "platform", {"banner": banner}, etag)
            assert saved.status_code == 200, saved.text
            public = await w.client.get(f"{SETTINGS}/public", headers=auth(w.admin))
            return public.json()["banner"]

        active = await banner_after(
            now - timedelta(hours=1), now + timedelta(hours=1), '"platform-r0"'
        )
        assert active is not None and active["text"] == "Heads up"
        assert active["severity"] == "info"
        expired = await banner_after(
            now - timedelta(hours=2), now - timedelta(hours=1), '"platform-r1"'
        )
        assert expired is None
        future = await banner_after(now + timedelta(hours=1), None, '"platform-r2"')
        assert future is None
        reversed_window = await put(
            w,
            w.admin,
            "platform",
            {
                "banner": {
                    "text": "x",
                    "starts_at": now.isoformat(),
                    "ends_at": (now - timedelta(hours=1)).isoformat(),
                }
            },
            '"platform-r3"',
        )
        assert reversed_window.status_code == 422


# --- export and import --------------------------------------------------------------------------


async def test_export_is_versioned_secret_free_and_round_trips_as_no_changes(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        await put(w, w.admin, "platform", {"banner": {"text": "Hello"}}, '"platform-r0"')
        resp = await w.client.get(f"{SETTINGS}/export", headers=auth(w.admin))
        assert resp.status_code == 200
        assert resp.headers["content-disposition"].startswith(
            'attachment; filename="contractum-settings-'
        )
        doc = resp.json()
        assert doc["format"] == "contractum-settings"
        assert doc["format_version"] == 1
        assert doc["sections"]["platform"] == {"banner": {"text": "Hello", "severity": "info"}}
        assert doc["sections"]["entra"] == {}
        assert "secret" not in json.dumps(doc).lower()
        dry = await post_import(w, w.admin, json.dumps(doc).encode())
        assert dry.status_code == 200, dry.text
        assert dry.json()["dry_run"] is True
        assert dry.json()["changes"] == []


async def test_import_dry_run_diffs_and_apply_is_one_audited_event(open_world: OpenWorld) -> None:
    async with open_world() as w:
        before = len(events(w))
        file = export_file(
            {
                "platform": {"banner": {"text": "Imported", "severity": "warning"}},
                "entra": {"default_tenant_id": ENTRA_TENANT_ID},
            }
        )
        dry = await post_import(w, w.admin, file)
        assert dry.status_code == 200, dry.text
        assert dry.json()["changes"] == [
            {
                "section": "entra",
                "field": "default_tenant_id",
                "before": None,
                "after": ENTRA_TENANT_ID,
            },
            {
                "section": "platform",
                "field": "banner",
                "before": None,
                "after": {"text": "Imported", "severity": "warning"},
            },
        ]
        assert len(events(w)) == before  # a dry run writes nothing
        etag = dry.json()["etag"]
        assert (await post_import(w, w.admin, file, dry_run=False)).status_code == 428
        assert (
            await post_import(w, w.admin, file, dry_run=False, etag='"all-stale"')
        ).status_code == 412
        applied = await post_import(w, w.admin, file, dry_run=False, etag=etag)
        assert applied.status_code == 200, applied.text
        assert applied.json()["dry_run"] is False
        assert len(applied.json()["changes"]) == 2
        found = events(w)
        assert [e["action"] for e in found] == ["settings.imported"]
        assert found[0]["aggregate_id"] == "import"
        assert found[0]["after"]["platform"]["banner"]["text"] == "Imported"
        assert set(found[0]["before"]) == {"entra", "platform"}
        # The old combined tag is stale now; applying the same file again is a no-op, no event.
        assert (await post_import(w, w.admin, file, dry_run=False, etag=etag)).status_code == 412
        fresh = await combined_etag(w)
        noop = await post_import(w, w.admin, file, dry_run=False, etag=fresh)
        assert noop.status_code == 200 and noop.json()["changes"] == []
        assert len(events(w)) == 1


@pytest.mark.parametrize(
    ("body", "pointer"),
    [
        (b"not json", ""),
        (b"[]", ""),
        (export_file({"nope": {}}), "/sections/nope"),
        (export_file({"entra": {"surprise": 1}}), "/sections/entra/surprise"),
        (
            export_file({"entra": {"default_tenant_id": new_guid()}}),
            "/sections/entra/default_tenant_id",
        ),
        (
            json.dumps(
                {"format": "contractum-settings", "format_version": 9, "sections": {}}
            ).encode(),
            "/format_version",
        ),
    ],
)
async def test_an_invalid_import_is_422_with_a_pointer(
    open_world: OpenWorld, body: bytes, pointer: str
) -> None:
    async with open_world() as w:
        resp = await post_import(w, w.admin, body)
        assert resp.status_code == 422, resp.text
        assert slug_of(resp) == "settings-import-invalid"
        assert resp.json()["errors"][0]["pointer"] == pointer
        assert events(w) == []


async def test_an_imported_entra_owner_that_cannot_own_points_at_the_imported_field(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        body = export_file({"entra": {"default_owner_folder_id": w.team_a}})
        resp = await post_import(w, w.admin, body)
        assert resp.status_code == 422, resp.text
        assert slug_of(resp) == "settings-import-invalid"
        assert [e["pointer"] for e in resp.json()["errors"]] == [
            "/sections/entra/default_owner_folder_id"
        ]
        assert w.team_a not in resp.text
        assert events(w) == []


async def test_import_limits_size_and_depth(open_world: OpenWorld) -> None:
    async with open_world() as w:
        huge = export_file({"platform": {"banner": {"text": "x" * (300 * 1024)}}})
        too_big = await post_import(w, w.admin, huge)
        assert too_big.status_code == 413
        bomb = b"[" * 5000 + b"]" * 5000
        deep = await post_import(w, w.admin, bomb)
        assert deep.status_code == 422
        assert slug_of(deep) == "settings-import-invalid"


# --- the runtime route --------------------------------------------------------------------------


async def test_the_runtime_route_serves_only_runtime_sections_to_a_service_account_key(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        await put(w, w.admin, "platform", {"banner": {"text": "Runtime"}}, '"platform-r0"')
        sa = await w.client.post(
            f"{IDENTITY}/service-accounts",
            json={"owner_folder_id": w.root, "name": "settings-runtime"},
            headers=auth(w.admin),
        )
        assert sa.status_code == 201, sa.text
        key = await w.client.post(
            f"{IDENTITY}/service-accounts/{sa.json()['id']}/keys",
            json={
                "name": "k",
                "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
            },
            headers=auth(w.admin),
        )
        assert key.status_code == 201, key.text
        token = key.json()["token"]
        ok = await w.client.get(RUNTIME, params={"sections": "platform"}, headers=auth(token))
        assert ok.status_code == 200, ok.text
        assert ok.json() == {
            "sections": {"platform": {"banner": {"text": "Runtime", "severity": "info"}}}
        }
        for sections in ("entra", "platform,entra", "nope", ",", "platform,platform"):
            bad = await w.client.get(RUNTIME, params={"sections": sections}, headers=auth(token))
            assert bad.status_code == 422, (sections, bad.text)
        assert (await w.client.get(RUNTIME, params={"sections": "platform"})).status_code == 401


# --- the Entra overview and the connection test --------------------------------------------------


async def test_the_overview_is_view_only_and_never_carries_a_secret(
    open_world: OpenWorld, tmp_path: Path
) -> None:
    secret_file = tmp_path / "graph_secret"
    secret_file.write_text("super-secret-value\n", encoding="utf-8")
    async with open_world(
        graph_client_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        graph_client_secret_file=secret_file,
    ) as w:
        w.directory.secret_expiries = [
            datetime.now(UTC) + timedelta(days=100),
            datetime.now(UTC) + timedelta(days=40),
        ]
        resp = await w.client.get(f"{SETTINGS}/entra/overview", headers=auth(w.admin))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["graph_configured"] is True
        assert body["graph_secret_configured"] is True
        assert body["signin_configured"] is True
        assert body["graph_client_id"] == "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        assert ENTRA_TENANT_ID in [t["tenant_id"] for t in body["tenants"]]
        assert body["secret_expiry"]["status"] == "known"
        assert 99 <= body["secret_expiry"]["days_left"] <= 100
        assert "super-secret-value" not in resp.text
        assert "hint" not in resp.text.lower()


async def test_an_unknown_expiry_is_reported_as_unknown(open_world: OpenWorld) -> None:
    async with open_world(graph_client_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee") as w:
        w.directory.secret_expiries = None
        resp = await w.client.get(f"{SETTINGS}/entra/overview", headers=auth(w.admin))
        assert resp.json()["secret_expiry"]["status"] == "unknown"
        assert resp.json()["secret_expiry"]["reason"]
        assert resp.json()["graph_secret_configured"] is False


async def test_the_connection_test_answers_a_fixed_code_and_is_rate_limited(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        ok = await w.client.post(f"{SETTINGS}/entra/test", headers=auth(w.admin))
        assert ok.status_code == 200, ok.text
        assert ok.json()["result"] == "ok"
        assert ok.json()["tenant_id"] == ENTRA_TENANT_ID
        w.directory.connection_result = "token-refused"
        refused = await w.client.post(f"{SETTINGS}/entra/test", headers=auth(w.admin))
        assert refused.json()["result"] == "token-refused"
        for _ in range(4):
            assert (
                await w.client.post(f"{SETTINGS}/entra/test", headers=auth(w.admin))
            ).status_code == 200
        limited = await w.client.post(f"{SETTINGS}/entra/test", headers=auth(w.admin))
        assert limited.status_code == 429
        assert slug_of(limited) == "too-many-attempts"
        assert int(limited.headers["retry-after"]) >= 1
        assert events(w) == []  # a test changes no state and is not audited


# --- About --------------------------------------------------------------------------------------


def _about_answer(service: str) -> dict[str, Any]:
    return {
        "service": service,
        "version": "0.1.0",
        "commit": "abc1234",
        "built_at": "2026-11-20T10:00:00Z",
        "database": "ok",
        "migration_head": f"{service}_head",
        "setting_defaults": {},
    }


@respx.mock
async def test_about_lists_five_services_and_marks_an_unreachable_one(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        seen: list[str] = []

        def _answer(name: str) -> Any:
            def _handler(request: httpx.Request) -> httpx.Response:
                seen.append(request.headers["authorization"])
                return httpx.Response(200, json=_about_answer(name))

            return _handler

        for name in ("contracts", "validation", "audit"):
            respx.get(f"http://{name}:8000/internal/v1/about").mock(side_effect=_answer(name))
        respx.get("http://mail:8000/internal/v1/about").mock(side_effect=httpx.ConnectError("down"))
        resp = await w.client.get(f"{SETTINGS}/about", headers=auth(w.admin))
        assert resp.status_code == 200, resp.text
        services = {s["service"]: s for s in resp.json()["services"]}
        assert list(services) == ["identity", "contracts", "validation", "audit", "mail"]
        assert services["identity"]["status"] == "ok"
        assert services["identity"]["database"] == "ok"
        assert services["identity"]["migration_head"] == "0014_settings_seed"
        assert services["contracts"]["migration_head"] == "contracts_head"
        assert services["mail"]["status"] == "unreachable"
        assert services["mail"]["version"] is None
        assert seen and all(h == f"Bearer {w.admin}" for h in seen)  # the caller's own bearer


# --- notifications (quick 261120-0yd) ------------------------------------------------------------

SUBJECT_KEY = "contract.version.published"


async def _service_account_token(w: Any) -> str:
    sa = await w.client.post(
        f"{IDENTITY}/service-accounts",
        json={"owner_folder_id": w.root, "name": "notifier-like"},
        headers=auth(w.admin),
    )
    assert sa.status_code == 201, sa.text
    key = await w.client.post(
        f"{IDENTITY}/service-accounts/{sa.json()['id']}/keys",
        json={"name": "k", "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat()},
        headers=auth(w.admin),
    )
    assert key.status_code == 201, key.text
    return str(key.json()["token"])


async def test_a_valid_notifications_save_is_one_event_and_served_to_a_service_account_key(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        value = {
            "email_templates": {SUBJECT_KEY: {"subject": "{card}: {odcs} {semver} is out"}},
            "sender": {"name": "Contractum", "address": "no-reply@example.test"},
            "retry_schedule_s": [30, 120],
            "max_attempts": 4,
            "sunset_lead_days": [14, 3],
            "webhook_allowed_hosts": ["*.example.org", "hooks.example.com"],
        }
        resp = await put(w, w.admin, "notifications", value, '"notifications-r0"')
        assert resp.status_code == 200, resp.text
        stored = resp.json()["values"]
        assert stored["sunset_lead_days"] == [3, 14]
        assert stored["email_templates"] == value["email_templates"]
        found = events(w)
        assert len(found) == 1
        assert found[0]["action"] == "settings.updated"
        assert found[0]["aggregate_id"] == "notifications"
        assert found[0]["after"] == {"notifications": stored}
        assert [str(i) for i in found[0]["scope_path"]] == [w.root]
        again = await put(w, w.admin, "notifications", value, resp.headers["etag"])
        assert again.status_code == 200
        assert len(events(w)) == 1  # the same values: no event

        token = await _service_account_token(w)
        runtime = await w.client.get(
            RUNTIME, params={"sections": "notifications"}, headers=auth(token)
        )
        assert runtime.status_code == 200, runtime.text
        assert runtime.json() == {"sections": {"notifications": stored}}


async def test_a_bad_placeholder_is_422_at_the_field_and_nothing_is_written(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        resp = await put(
            w,
            w.admin,
            "notifications",
            {"email_templates": {SUBJECT_KEY: {"subject": "Hi {bogus}"}}},
            '"notifications-r0"',
        )
        assert resp.status_code == 422, resp.text
        problem = resp.json()
        assert problem["type"] == "urn:dc:problem:validation-error"
        assert problem["errors"][0]["pointer"] == f"/email_templates/{SUBJECT_KEY}/subject"
        assert "Hi {bogus}" not in resp.text
        assert w.sql(w.env.db_url, "SELECT 1 FROM platform_setting") == []
        assert events(w) == []


@pytest.mark.parametrize(
    "value,pointer",
    [
        ({"webhook_allowed_hosts": ["10.0.0.1"]}, "/webhook_allowed_hosts"),
        ({"webhook_allowed_hosts": ["example.com:443"]}, "/webhook_allowed_hosts"),
        ({"sender": {"address": "Name <a@b.com>"}}, "/sender/address"),
        ({"sender": {"name": "a\nb"}}, "/sender/name"),
        ({"sender": {"name": "Team\u202e"}}, "/sender/name"),
        ({"sender": {"name": "Ops \U0001f600"}}, "/sender/name"),
        ({"retry_schedule_s": [5]}, "/retry_schedule_s/0"),
        ({"max_attempts": 99}, "/max_attempts"),
        ({"sunset_lead_days": [7, 7]}, "/sunset_lead_days"),
        ({"email_templates": {"nope": {"subject": "x"}}}, "/email_templates"),
        (
            {"email_templates": {SUBJECT_KEY: {"body": "{odcs.__class__}"}}},
            f"/email_templates/{SUBJECT_KEY}/body",
        ),
    ],
)
async def test_notification_refusals_carry_a_pointer(
    open_world: OpenWorld, value: dict[str, Any], pointer: str
) -> None:
    async with open_world() as w:
        resp = await put(w, w.admin, "notifications", value, '"notifications-r0"')
        assert resp.status_code == 422, resp.text
        assert resp.json()["errors"][0]["pointer"] == pointer
        assert events(w) == []


async def test_preview_renders_the_default_and_an_edited_template_with_the_sample(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        url = f"{SETTINGS}/notifications/preview"
        default = await w.client.post(url, json={"event_type": SUBJECT_KEY}, headers=auth(w.admin))
        assert default.status_code == 200, default.text
        out = default.json()
        assert out["subject"] == "[Contractum] orders 1.2.0 published"
        assert out["body"].startswith("[Contractum] orders 1.2.0 published\n\nCard: Analytics team")
        assert out["body"].endswith("has the email channel on.\n")
        edited = await w.client.post(
            url,
            json={
                "event_type": "contract.version.breaking_published",
                "subject": "{card}: {odcs} {semver} breaks things",
                "body": "{subject}\n{details}{breaking_changes}",
            },
            headers=auth(w.admin),
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["subject"] == "Analytics team: orders 1.2.0 breaks things"
        assert "Breaking changes for the versions your card pins:" in edited.json()["body"]
        assert len(edited.json()["body"].splitlines()) == 7
        invalid = await w.client.post(
            url,
            json={"event_type": SUBJECT_KEY, "subject": "{nope}"},
            headers=auth(w.admin),
        )
        assert invalid.status_code == 422
        assert invalid.json()["errors"][0]["pointer"] == "/subject"
        invalid_body = await w.client.post(
            url, json={"event_type": SUBJECT_KEY, "body": "{"}, headers=auth(w.admin)
        )
        assert invalid_body.json()["errors"][0]["pointer"] == "/body"
        unknown = await w.client.post(url, json={"event_type": "bogus"}, headers=auth(w.admin))
        assert unknown.status_code == 422
        editor = await w.local_token("preview-editor", "team_editor", w.team_a)
        denied = await w.client.post(url, json={"event_type": SUBJECT_KEY}, headers=auth(editor))
        assert denied.status_code == 403
        assert events(w) == []  # a preview changes no state and writes no event


async def test_notifications_export_and_import_round_trip_with_templates(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        value = {
            "email_templates": {"generic": {"subject": "{card} has news", "body": "{details}"}},
            "webhook_allowed_hosts": ["hooks.example.com"],
        }
        saved = await put(w, w.admin, "notifications", value, '"notifications-r0"')
        assert saved.status_code == 200, saved.text
        exported = await w.client.get(f"{SETTINGS}/export", headers=auth(w.admin))
        file = exported.json()
        assert file["sections"]["notifications"] == value
        dry = await post_import(w, w.admin, json.dumps(file).encode())
        assert dry.status_code == 200, dry.text
        assert dry.json()["changes"] == []
        changed = json.loads(json.dumps(file))
        changed["sections"]["notifications"]["max_attempts"] = 5
        applied = await post_import(
            w, w.admin, json.dumps(changed).encode(), dry_run=False, etag=await combined_etag(w)
        )
        assert applied.status_code == 200, applied.text
        assert [c["field"] for c in applied.json()["changes"]] == ["max_attempts"]
        bad = json.loads(json.dumps(file))
        bad["sections"]["notifications"]["webhook_allowed_hosts"] = ["10.0.0.1"]
        refused = await post_import(w, w.admin, json.dumps(bad).encode())
        assert refused.status_code == 422
        assert refused.json()["type"] == "urn:dc:problem:settings-import-invalid"
        assert (
            refused.json()["errors"][0]["pointer"]
            == "/sections/notifications/webhook_allowed_hosts"
        )


@respx.mock
async def test_notification_defaults_come_from_contracts_and_mail_about(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        contracts_defaults = {
            "email_templates": {SUBJECT_KEY: {"subject": "S", "body": "B"}},
            "retry_schedule_s": [11, 22],
            "max_attempts": 3,
            "sunset_lead_days": [5],
            "webhook_allowed_hosts": [],
        }
        seen: list[str] = []

        def _contracts(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["authorization"])
            about = _about_answer("contracts")
            about["setting_defaults"] = {
                "notifications": contracts_defaults,
                "governance": {
                    "default_odcs_api_version": "v3.1.0",
                    "require_lifecycle_reason": True,
                    "odcs_api_versions": ["v3.1.0", "v3.2.0"],
                },
            }
            return httpx.Response(200, json=about)

        def _validation(request: httpx.Request) -> httpx.Response:
            about = _about_answer("validation")
            about["setting_defaults"] = {"governance": {"mandatory_warn_days": 9}}
            return httpx.Response(200, json=about)

        def _audit(request: httpx.Request) -> httpx.Response:
            about = _about_answer("audit")
            about["setting_defaults"] = {"platform": {"audit_retention_days": 365}}
            return httpx.Response(200, json=about)

        def _mail(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["authorization"])
            about = _about_answer("mail")
            about["setting_defaults"] = {
                "notifications": {"sender": {"name": None, "address": "dc@corp.example.test"}}
            }
            return httpx.Response(200, json=about)

        respx.get("http://contracts:8000/internal/v1/about").mock(side_effect=_contracts)
        respx.get("http://mail:8000/internal/v1/about").mock(side_effect=_mail)
        respx.get("http://validation:8000/internal/v1/about").mock(side_effect=_validation)
        respx.get("http://audit:8000/internal/v1/about").mock(side_effect=_audit)
        listed = await w.client.get(SETTINGS, headers=auth(w.admin))
        assert listed.status_code == 200, listed.text
        sections = {s["section"]: s for s in listed.json()["sections"]}
        # quick 261120-0ye (D-43): governance from validation and contracts, platform from audit,
        # security from identity's own env
        assert sections["governance"]["defaults_source"] == "service"
        assert sections["governance"]["defaults"] == {
            "mandatory_warn_days": 9,
            "default_odcs_api_version": "v3.1.0",
            "require_lifecycle_reason": True,
        }
        assert sections["platform"]["defaults"]["audit_retention_days"] == 365
        assert sections["security"]["defaults_source"] == "identity"
        assert sections["security"]["defaults"]["credential_max_ttl_days"] == 365
        assert sections["security"]["defaults"]["session_max_s"] == 604800
        assert sections["security"]["defaults"]["password_min_length"] == 12
        section = sections["notifications"]
        assert section["defaults_source"] == "service"
        assert section["defaults"]["retry_schedule_s"] == [11, 22]
        assert section["defaults"]["max_attempts"] == 3
        assert section["defaults"]["sunset_lead_days"] == [5]
        assert section["defaults"]["sender"] == {"name": None, "address": "dc@corp.example.test"}
        assert section["defaults"]["email_templates"][SUBJECT_KEY] == {"subject": "S", "body": "B"}
        assert seen and all(h == f"Bearer {w.admin}" for h in seen)


@respx.mock
async def test_notification_defaults_fall_back_to_the_builtin_ones_when_a_service_is_down(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        for name in ("contracts", "mail", "validation", "audit"):
            respx.get(f"http://{name}:8000/internal/v1/about").mock(
                side_effect=httpx.ConnectError("down")
            )
        listed = await w.client.get(SETTINGS, headers=auth(w.admin))
        sections = {s["section"]: s for s in listed.json()["sections"]}
        assert sections["governance"]["defaults_source"] == "built-in"
        assert sections["governance"]["defaults"]["mandatory_warn_days"] is None
        section = sections["notifications"]
        assert section["defaults_source"] == "built-in"
        assert section["defaults"]["retry_schedule_s"] == [60, 300, 1800, 7200, 21600, 43200]
        assert section["defaults"]["max_attempts"] == 7
        assert section["defaults"]["sunset_lead_days"] == [7, 30]
        assert section["defaults"]["sender"] == {"name": None, "address": None}
        assert section["defaults"]["email_templates"][SUBJECT_KEY]["subject"] == (
            "[Contractum] {odcs} {semver} published"
        )


# --- governance, security and retention (quick 261120-0ye) -----------------------------


async def test_governance_is_saved_audited_served_to_services_and_refuses_bad_values(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        saved = await put(
            w,
            w.admin,
            "governance",
            {
                "mandatory_warn_days": 3,
                "default_odcs_api_version": "v3.1.0",
                "require_lifecycle_reason": True,
            },
            '"governance-r0"',
        )
        assert saved.status_code == 200, saved.text
        found = events(w)
        assert [e["action"] for e in found] == ["settings.updated"]
        assert found[0]["after"]["governance"]["mandatory_warn_days"] == 3

        listing = await w.client.get(SETTINGS, headers=auth(w.admin))
        assert listing.json()["choices"]["odcs_api_versions"] == ["v3.1.0", "v3.2.0"]
        assert listing.json()["choices"]["microsoft_signin_available"] is True
        token = await _service_account_token(w)
        runtime = await w.client.get(
            RUNTIME, params={"sections": "governance"}, headers=auth(token)
        )
        assert runtime.status_code == 200, runtime.text
        assert runtime.json()["sections"]["governance"]["mandatory_warn_days"] == 3

        for body, pointer in (
            ({"mandatory_warn_days": 366}, "/mandatory_warn_days"),
            ({"default_odcs_api_version": "v9.9.9"}, "/default_odcs_api_version"),  # not vendored
            ({"default_odcs_api_version": "3.2.0"}, "/default_odcs_api_version"),
            ({"nope": 1}, "/nope"),
        ):
            bad = await put(w, w.admin, "governance", body, saved.headers["etag"])
            assert bad.status_code == 422, (body, bad.text)
            assert bad.json()["errors"][0]["pointer"] == pointer
        assert len(events(w)) == 1


async def test_the_security_section_is_never_served_to_other_services(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        saved = await put(w, w.admin, "security", {"password_min_length": 14}, '"security-r0"')
        assert saved.status_code == 200, saved.text
        token = await _service_account_token(w)
        for sections in ("security", "governance,security"):
            bad = await w.client.get(RUNTIME, params={"sections": sections}, headers=auth(token))
            assert bad.status_code == 422, (sections, bad.text)


async def test_security_bounds_are_refused_with_a_pointer_and_nothing_is_stored(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        for body, pointer in (
            ({"password_min_length": 11}, "/password_min_length"),
            ({"credential_max_ttl_days": 3651}, "/credential_max_ttl_days"),
            ({"lockout_account_threshold": 2}, "/lockout_account_threshold"),
            ({"session_max_s": 100}, "/session_max_s"),
            ({"session_max_s": 3600, "idle_timeout_s": 7200}, "/idle_timeout_s"),
            ({"max_active_tokens": 0}, "/max_active_tokens"),
            ({"max_active_tokens": 1001}, "/max_active_tokens"),
            ({"max_active_tokens": "5"}, "/max_active_tokens"),
            ({"max_sessions_per_user": 0}, "/max_sessions_per_user"),
            ({"max_sessions_per_user": 101}, "/max_sessions_per_user"),
            ({"max_sessions_per_user": "5"}, "/max_sessions_per_user"),
        ):
            bad = await put(w, w.admin, "security", body, '"security-r0"')
            assert bad.status_code == 422, (body, bad.text)
            assert bad.json()["errors"][0]["pointer"] == pointer
        assert events(w) == []
        assert w.sql(w.env.db_url, "SELECT * FROM platform_setting") == []


async def test_platform_retention_is_validated_and_exported_with_the_new_sections(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        for days in (-1, 1, 89, 36501):
            bad = await put(w, w.admin, "platform", {"audit_retention_days": days}, '"platform-r0"')
            assert bad.status_code == 422, (days, bad.text)
            assert bad.json()["errors"][0]["pointer"] == "/audit_retention_days"
        assert (
            await put(w, w.admin, "platform", {"audit_retention_days": 400}, '"platform-r0"')
        ).status_code == 200
        assert (
            await put(w, w.admin, "governance", {"mandatory_warn_days": 5}, '"governance-r0"')
        ).status_code == 200
        assert (
            await put(w, w.admin, "security", {"password_min_length": 16}, '"security-r0"')
        ).status_code == 200

        doc = (await w.client.get(f"{SETTINGS}/export", headers=auth(w.admin))).json()
        assert doc["sections"]["platform"] == {"audit_retention_days": 400}
        assert doc["sections"]["governance"] == {"mandatory_warn_days": 5}
        assert doc["sections"]["security"] == {"password_min_length": 16}

        changed = json.loads(json.dumps(doc))
        changed["sections"]["governance"]["mandatory_warn_days"] = 6
        changed["sections"]["platform"]["audit_retention_days"] = 0
        dry = await post_import(w, w.admin, json.dumps(changed).encode())
        assert dry.status_code == 200, dry.text
        assert {(c["section"], c["field"]) for c in dry.json()["changes"]} == {
            ("governance", "mandatory_warn_days"),
            ("platform", "audit_retention_days"),
        }
        bad = json.loads(json.dumps(doc))
        bad["sections"]["governance"]["default_odcs_api_version"] = "v9.9.9"
        refused = await post_import(w, w.admin, json.dumps(bad).encode())
        assert refused.status_code == 422, refused.text
        assert refused.json()["errors"][0]["pointer"] == (
            "/sections/governance/default_odcs_api_version"
        )


async def test_the_public_view_carries_the_effective_governance_and_security_values(
    open_world: OpenWorld,
) -> None:
    async with open_world() as w:
        reader = await w.local_token("pub-reader", "global_reader", w.root)
        before = await w.client.get(f"{SETTINGS}/public", headers=auth(reader))
        assert before.status_code == 200
        body = before.json()
        assert body["default_odcs_api_version"] == "v3.2.0"
        assert body["require_lifecycle_reason"] is False
        assert body["credential_max_ttl_days"] == 365
        assert body["password_rules"] == {
            "min_length": 12,
            "require_upper": False,
            "require_lower": False,
            "require_digit": False,
            "require_symbol": False,
        }
        await put(
            w,
            w.admin,
            "governance",
            {"default_odcs_api_version": "v3.1.0", "require_lifecycle_reason": True},
            '"governance-r0"',
        )
        await put(
            w,
            w.admin,
            "security",
            {
                "credential_max_ttl_days": 90,
                "password_min_length": 15,
                "password_require_digit": True,
            },
            '"security-r0"',
        )
        w.env.app.state.platform_settings.clear()
        after = (await w.client.get(f"{SETTINGS}/public", headers=auth(reader))).json()
        assert after["default_odcs_api_version"] == "v3.1.0"
        assert after["require_lifecycle_reason"] is True
        assert after["credential_max_ttl_days"] == 90
        assert after["password_rules"]["min_length"] == 15
        assert after["password_rules"]["require_digit"] is True


async def test_the_active_token_limit_defaults_to_ten_stores_1_and_1000_and_is_audited(
    open_world: OpenWorld,
) -> None:
    """Quick 261120-lbz, D-03: bounds 1 to 1000, one settings.updated event, export and import."""
    async with open_world() as w:
        view = await w.client.get(SETTINGS, headers=auth(w.admin))
        assert view.status_code == 200, view.text
        section = next(s for s in view.json()["sections"] if s["section"] == "security")
        assert section["defaults"]["max_active_tokens"] == 10

        first = await put(w, w.admin, "security", {"max_active_tokens": 1}, '"security-r0"')
        assert first.status_code == 200, first.text
        second = await put(w, w.admin, "security", {"max_active_tokens": 1000}, '"security-r1"')
        assert second.status_code == 200, second.text
        changed = events(w)
        assert [e["action"] for e in changed] == ["settings.updated", "settings.updated"]
        assert changed[-1]["after"] == {"security": {"max_active_tokens": 1000}}

        doc = (await w.client.get(f"{SETTINGS}/export", headers=auth(w.admin))).json()
        assert doc["sections"]["security"] == {"max_active_tokens": 1000}
        edited = json.loads(json.dumps(doc))
        edited["sections"]["security"]["max_active_tokens"] = 25
        dry = await post_import(w, w.admin, json.dumps(edited).encode())
        assert dry.status_code == 200, dry.text
        assert {(c["section"], c["field"]) for c in dry.json()["changes"]} == {
            ("security", "max_active_tokens")
        }
        too_big = json.loads(json.dumps(doc))
        too_big["sections"]["security"]["max_active_tokens"] = 1001
        refused = await post_import(w, w.admin, json.dumps(too_big).encode())
        assert refused.status_code == 422, refused.text
        assert refused.json()["errors"][0]["pointer"] == "/sections/security/max_active_tokens"


async def test_the_env_limit_is_the_default_and_must_lie_between_1_and_1000(
    open_world: OpenWorld,
) -> None:
    from dc_identity.settings import IdentitySettings

    for bad in (0, 1001):
        with pytest.raises(ValueError, match="max_active_tokens"):
            IdentitySettings(
                database_url="postgresql+psycopg://x@h/d",  # type: ignore[arg-type]
                max_active_tokens=bad,
            )
    async with open_world(max_active_tokens=7) as w:
        view = await w.client.get(SETTINGS, headers=auth(w.admin))
        section = next(s for s in view.json()["sections"] if s["section"] == "security")
        assert section["defaults"]["max_active_tokens"] == 7


async def test_the_session_limit_defaults_to_five_stores_1_and_100_and_is_audited(
    open_world: OpenWorld,
) -> None:
    """Quick 261120-s0p, D-09: bounds 1 to 100, one settings.updated event per change, env bound."""
    from dc_identity.settings import IdentitySettings

    for bad in (0, 101):
        with pytest.raises(ValueError, match="max_sessions_per_user"):
            IdentitySettings(
                database_url="postgresql+psycopg://x@h/d",  # type: ignore[arg-type]
                max_sessions_per_user=bad,
            )
    async with open_world() as w:
        view = await w.client.get(SETTINGS, headers=auth(w.admin))
        section = next(s for s in view.json()["sections"] if s["section"] == "security")
        assert section["defaults"]["max_sessions_per_user"] == 5
        first = await put(w, w.admin, "security", {"max_sessions_per_user": 1}, '"security-r0"')
        assert first.status_code == 200, first.text
        second = await put(w, w.admin, "security", {"max_sessions_per_user": 100}, '"security-r1"')
        assert second.status_code == 200, second.text
        changed = events(w)
        assert [e["action"] for e in changed] == ["settings.updated", "settings.updated"]
        assert changed[-1]["after"] == {"security": {"max_sessions_per_user": 100}}


async def test_the_env_session_limit_is_the_default_shown(open_world: OpenWorld) -> None:
    async with open_world(max_sessions_per_user=9) as w:
        view = await w.client.get(SETTINGS, headers=auth(w.admin))
        section = next(s for s in view.json()["sections"] if s["section"] == "security")
        assert section["defaults"]["max_sessions_per_user"] == 9
