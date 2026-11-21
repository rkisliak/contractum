"""GET /api/v1/audit/events and /events/{event_id}: the public, folder-scoped audit query (AUD-05).

Identity is mocked with respx: ``/check`` answers the ``audit:list`` gate and
``/accessible-folders`` answers the ``audit:query`` scope. Events are seeded through the real
ingest (``handle_payload``) into a migrated audit database.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from dc_audit.ingest import handle_payload
from dc_audit.main import create_audit_app
from dc_audit.settings import AuditSettings
from dc_auth.issuers import AuthSettings
from dc_auth.testing import FakeIssuer
from dc_core.events import encode_envelope, subject_for

pytestmark = pytest.mark.integration

CHECK = "http://identity:8000/internal/v1/authz/check"
FOLDERS = "http://identity:8000/internal/v1/authz/accessible-folders"
EVENTS = "/api/v1/audit/events"

ROOT = uuid.UUID("00000000-0000-7000-8000-000000000001")
ORG = uuid.UUID("00000000-0000-7000-8000-0000000000a1")
PROJECT = uuid.UUID("00000000-0000-7000-8000-0000000000b1")
TEAM = uuid.UUID("00000000-0000-7000-8000-0000000000c1")
ORG2 = uuid.UUID("00000000-0000-7000-8000-0000000000a2")
PROJECT2 = uuid.UUID("00000000-0000-7000-8000-0000000000b2")
TEAM2 = uuid.UUID("00000000-0000-7000-8000-0000000000c2")
BASE = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def event(
    *,
    scope_path: list[uuid.UUID],
    at: datetime = BASE,
    service: str = "contracts",
    aggregate_type: str = "contract",
    aggregate_id: str = "c-1",
    action: str = "contract.created",
    actor_id: str = "fake:author",
    prefix: str = "audit",
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "occurred_at": at.isoformat(),
        "service": service,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "aggregate_version": None,
        "scope_path": [str(f) for f in scope_path],
        "action": action,
        "actor": {"type": "user", "id": actor_id, "display": "Author", "via": "jwt"},
        "request_id": "req-1",
        "before": None,
        "after": None if prefix == "access" else {"id": aggregate_id},
        "schema_version": 1,
        "_prefix": prefix,
    }
    return envelope


async def seed(audit_env: Any, *events: dict[str, Any]) -> list[dict[str, Any]]:
    for item in events:
        body = {k: v for k, v in item.items() if k != "_prefix"}
        subject = subject_for(
            body["service"], body["aggregate_type"], body["action"], prefix=item["_prefix"]
        )
        assert await handle_payload(
            audit_env.session_factory, encode_envelope(body), subject=subject
        )
    return list(events)


@pytest.fixture
def app(audit_env: Any, fake_issuer: FakeIssuer):  # type: ignore[no-untyped-def]
    settings = AuditSettings(database_url=audit_env.db_url)  # type: ignore[arg-type]
    auth = AuthSettings(
        issuers=[fake_issuer.issuer_config()], identity_internal_url="http://identity:8000"
    )
    return create_audit_app(settings, auth)


def auth(fake_issuer: FakeIssuer, sub: str = "org-admin") -> dict[str, str]:
    return {"Authorization": f"Bearer {fake_issuer.mint(sub)}"}


def scope(
    *, all_: bool = False, folder_ids: list[uuid.UUID] = (), excluded: list[uuid.UUID] = ()
) -> dict[str, Any]:  # type: ignore[assignment]
    return {
        "all": all_,
        "folder_ids": [str(f) for f in folder_ids],
        "excluded_subtrees": [str(f) for f in excluded],
    }


async def query(
    app: Any,
    asgi_client: Any,
    fake_issuer: FakeIssuer,
    the_scope: dict[str, Any],
    path: str = EVENTS,
    **params: Any,
) -> httpx.Response:
    # Not every request reaches the scope lookup (a 422 parameter is refused before it).
    with respx.mock(assert_all_called=False) as router:
        router.post(CHECK).respond(json={"outcome": "allow", "folder": None})
        folders = router.post(FOLDERS).respond(json=the_scope)
        async with asgi_client(app) as client:
            response: httpx.Response = await client.get(
                path, params=params, headers=auth(fake_issuer)
            )
    if response.status_code == 200:
        assert json.loads(folders.calls.last.request.content) == {"permission": "audit:query"}
    return response


T1 = [ROOT, ORG, PROJECT, TEAM]
T2 = [ROOT, ORG2, PROJECT2, TEAM2]


@pytest.fixture
async def seeded(audit_env: Any) -> dict[str, dict[str, Any]]:
    first = event(scope_path=T1, at=BASE, action="contract.created")
    second = event(scope_path=T1, at=BASE + timedelta(minutes=1), action="contract.draft_updated")
    other_contract = event(scope_path=T1, at=BASE, aggregate_id="c-9")
    other_org = event(scope_path=T2, at=BASE, aggregate_id="c-1")
    root_scoped = event(
        scope_path=[ROOT],
        at=BASE,
        service="identity",
        aggregate_type="user",
        aggregate_id="u-1",
        action="user.created",
    )
    unscoped = event(
        scope_path=[],
        at=BASE,
        service="identity",
        aggregate_type="pat",
        aggregate_id="p-1",
        action="pat.created",
    )
    await seed(audit_env, first, second, other_contract, other_org, root_scoped, unscoped)
    return {
        "first": first,
        "second": second,
        "other_contract": other_contract,
        "other_org": other_org,
        "root_scoped": root_scoped,
        "unscoped": unscoped,
    }


def ids(response: httpx.Response) -> list[str]:
    return [item["event_id"] for item in response.json()["items"]]


async def test_an_org_admin_sees_only_the_contract_events_of_its_folders_newest_first(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer, seeded: dict[str, dict[str, Any]]
) -> None:
    response = await query(
        app,
        asgi_client,
        fake_issuer,
        scope(folder_ids=[ORG, PROJECT, TEAM]),
        aggregate_type="contract",
        aggregate_id="c-1",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert ids(response) == [seeded["second"]["event_id"], seeded["first"]["event_id"]]
    assert body["next_cursor"] is None
    item = body["items"][0]
    assert set(item) == {
        "event_id",
        "occurred_at",
        "received_at",
        "service",
        "category",
        "aggregate_type",
        "aggregate_id",
        "aggregate_version",
        "action",
        "actor",
        "request_id",
        "scope_path",
        "before",
        "after",
        "changes",
        "detail",
    }
    assert item["category"] == "change"
    assert item["scope_path"] == [str(f) for f in T1]
    assert item["after"] == {"id": "c-1"}  # snapshots exactly as stored (D-169)


async def test_a_global_scope_with_an_excluded_org_sees_everything_else(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer, seeded: dict[str, dict[str, Any]]
) -> None:
    response = await query(app, asgi_client, fake_issuer, scope(all_=True, excluded=[ORG2]))
    assert response.status_code == 200, response.text
    expected = {seeded[k]["event_id"] for k in seeded if k != "other_org"}
    assert set(ids(response)) == expected  # includes the [root] and the empty-scope events


async def test_a_folder_scope_never_sees_root_scoped_or_unscoped_events(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer, seeded: dict[str, dict[str, Any]]
) -> None:
    response = await query(
        app, asgi_client, fake_issuer, scope(folder_ids=[ORG, PROJECT, TEAM, ORG2])
    )
    assert response.status_code == 200
    assert seeded["root_scoped"]["event_id"] not in ids(response)
    assert seeded["unscoped"]["event_id"] not in ids(response)
    # The leaf decides: ORG2 is in scope, but the other-org event's leaf is TEAM2.
    assert seeded["other_org"]["event_id"] not in ids(response)


async def test_an_empty_scope_is_an_empty_page_not_a_403(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer, seeded: dict[str, dict[str, Any]]
) -> None:
    response = await query(app, asgi_client, fake_issuer, scope())
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


async def test_no_token_is_401_problem_and_never_asks_identity(app: Any, asgi_client: Any) -> None:
    with respx.mock(assert_all_called=False) as router:
        check = router.post(CHECK).respond(json={"outcome": "allow", "folder": None})
        folders = router.post(FOLDERS).respond(json=scope(all_=True))
        async with asgi_client(app) as client:
            response = await client.get(EVENTS)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert not check.called and not folders.called


@pytest.mark.parametrize("failure", ["status", "connect"])
async def test_an_identity_outage_on_the_scope_fails_closed_with_503(
    app: Any,
    asgi_client: Any,
    fake_issuer: FakeIssuer,
    seeded: dict[str, dict[str, Any]],
    failure: str,
) -> None:
    with respx.mock() as router:
        router.post(CHECK).respond(json={"outcome": "allow", "folder": None})
        if failure == "status":
            router.post(FOLDERS).respond(503)
        else:
            router.post(FOLDERS).mock(side_effect=httpx.ConnectError("identity is down"))
        async with asgi_client(app) as client:
            response = await client.get(EVENTS, headers=auth(fake_issuer))
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "urn:dc:problem:service-unavailable"
    assert "c-1" not in response.text


async def test_the_gate_asks_identity_for_audit_list(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer
) -> None:
    with respx.mock() as router:
        check = router.post(CHECK).respond(json={"outcome": "allow", "folder": None})
        router.post(FOLDERS).respond(json=scope())
        async with asgi_client(app) as client:
            response = await client.get(EVENTS, headers=auth(fake_issuer))
    assert response.status_code == 200
    assert json.loads(check.calls.last.request.content)["permission"] == "audit:list"


async def test_the_public_routes_are_in_the_openapi_document(app: Any, asgi_client: Any) -> None:
    async with asgi_client(app) as client:
        response = await client.get("/api/v1/audit/openapi.json")
    paths = response.json()["paths"]
    assert {"/api/v1/audit/events", "/api/v1/audit/events/{event_id}"} <= set(paths)
    assert not [p for p in paths if "/internal" in p]


# --- Task 2: filters, keyset cursor, event by id, validation ----------------------------------

SA = f"sa:{uuid.UUID(int=77)}"


@pytest.fixture
async def mixed(audit_env: Any) -> dict[str, dict[str, Any]]:
    pipeline_read = event(
        scope_path=T1,
        at=BASE + timedelta(minutes=5),
        action="read",
        actor_id=SA,
        prefix="access",
    )
    author_read = event(
        scope_path=T1, at=BASE + timedelta(minutes=6), action="read", prefix="access"
    )
    publish = event(
        scope_path=T1, at=BASE + timedelta(minutes=2), action="contract.version_published"
    )
    refused = event(
        scope_path=T1, at=BASE + timedelta(minutes=3), action="contract.version.publish_refused"
    )
    template = event(
        scope_path=T1,
        at=BASE + timedelta(minutes=4),
        aggregate_type="template",
        aggregate_id="t-1",
        action="template.published",
    )
    validation = event(
        scope_path=T1,
        at=BASE + timedelta(minutes=4),
        service="validation",
        aggregate_type="ruleset",
        aggregate_id="r-1",
        action="contract.version.checked",
    )
    await seed(audit_env, pipeline_read, author_read, publish, refused, template, validation)
    return {
        "pipeline_read": pipeline_read,
        "author_read": author_read,
        "publish": publish,
        "refused": refused,
        "template": template,
        "validation": validation,
    }


TEAM_SCOPE = scope(folder_ids=[ORG, PROJECT, TEAM])


async def test_actor_and_category_filters_answer_what_did_this_pipeline_read(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer, mixed: dict[str, dict[str, Any]]
) -> None:
    response = await query(
        app, asgi_client, fake_issuer, TEAM_SCOPE, actor_id=SA, category="access"
    )
    assert response.status_code == 200, response.text
    assert ids(response) == [mixed["pipeline_read"]["event_id"]]
    assert response.json()["items"][0]["category"] == "access"
    access = await query(app, asgi_client, fake_issuer, TEAM_SCOPE, category="access")
    assert set(ids(access)) == {
        mixed["pipeline_read"]["event_id"],
        mixed["author_read"]["event_id"],
    }


async def test_service_and_action_filters_combine_with_and_and_a_dot_is_a_prefix(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer, mixed: dict[str, dict[str, Any]]
) -> None:
    prefix = await query(app, asgi_client, fake_issuer, TEAM_SCOPE, action="contract.version.")
    assert set(ids(prefix)) == {mixed["refused"]["event_id"], mixed["validation"]["event_id"]}
    both = await query(
        app, asgi_client, fake_issuer, TEAM_SCOPE, action="contract.version.", service="contracts"
    )
    assert ids(both) == [mixed["refused"]["event_id"]]
    exact = await query(app, asgi_client, fake_issuer, TEAM_SCOPE, action="contract.version")
    assert ids(exact) == []
    wildcard = await query(app, asgi_client, fake_issuer, TEAM_SCOPE, action="contract%.")
    assert ids(wildcard) == []  # LIKE wildcards in the prefix match only themselves


async def test_the_time_window_is_half_open(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer, mixed: dict[str, dict[str, Any]]
) -> None:
    start = (BASE + timedelta(minutes=2)).isoformat()
    end = (BASE + timedelta(minutes=4)).isoformat()
    window = await query(app, asgi_client, fake_issuer, TEAM_SCOPE, **{"from": start, "to": end})
    assert window.status_code == 200, window.text
    # The event at exactly `from` is in, the two at exactly `to` are out.
    assert ids(window) == [mixed["refused"]["event_id"], mixed["publish"]["event_id"]]
    empty = await query(app, asgi_client, fake_issuer, TEAM_SCOPE, **{"from": start, "to": start})
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-11-09T12:00:00"},
        {"to": "2026-11-09T12:00:00"},
        {"from": "2026-11-09T13:00:00+00:00", "to": "2026-11-09T12:00:00+00:00"},
        {"limit": 0},
        {"limit": 201},
        {"category": "write"},
        {"cursor": "bm90LWEtY3Vyc29y"},
        {"cursor": "!!"},
        {"aggregate_id": "a\x00b"},
        {"actor_id": "a\x00b"},
        {"service": "a\x00b"},
        {"action": "a\x00b"},
        {"aggregate_type": "a\x00b"},
        {"aggregate_id": "x" * 257},
    ],
)
async def test_bad_query_parameters_are_422_validation_error(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer, params: dict[str, Any]
) -> None:
    response = await query(app, asgi_client, fake_issuer, TEAM_SCOPE, **params)
    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "urn:dc:problem:validation-error"
    assert "\x00" not in response.text


async def _page_through(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer, limit: int, **params: Any
) -> list[list[str]]:
    pages: list[list[str]] = []
    cursor: str | None = None
    while True:
        extra = {"cursor": cursor} if cursor else {}
        response = await query(
            app, asgi_client, fake_issuer, TEAM_SCOPE, limit=limit, **params, **extra
        )
        assert response.status_code == 200, response.text
        pages.append(ids(response))
        cursor = response.json()["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) < 50


async def test_exactly_limit_rows_give_no_cursor_and_limit_plus_one_give_one_more_page(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer, audit_env: Any
) -> None:
    events = await seed(
        audit_env,
        *(event(scope_path=T1, at=BASE + timedelta(seconds=i), aggregate_id="p") for i in range(4)),
    )
    newest_first = [e["event_id"] for e in reversed(events)]
    assert await _page_through(app, asgi_client, fake_issuer, 4, aggregate_id="p") == [newest_first]
    assert await _page_through(app, asgi_client, fake_issuer, 3, aggregate_id="p") == [
        newest_first[:3],
        newest_first[3:],
    ]


async def test_equal_times_are_ordered_by_id_descending_with_no_repeat_or_gap(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer, audit_env: Any
) -> None:
    events = await seed(
        audit_env, *(event(scope_path=T1, at=BASE, aggregate_id="same") for _ in range(7))
    )
    rows = await audit_env.fetch(
        "SELECT event_id FROM audit_event WHERE aggregate_id = 'same' ORDER BY id DESC"
    )
    by_id_desc = [str(r["event_id"]) for r in rows]
    assert set(by_id_desc) == {e["event_id"] for e in events}
    pages = await _page_through(app, asgi_client, fake_issuer, 2, aggregate_id="same")
    assert [len(p) for p in pages] == [2, 2, 2, 1]
    assert [i for page in pages for i in page] == by_id_desc


async def test_one_event_by_id_and_the_uniform_404(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer, seeded: dict[str, dict[str, Any]]
) -> None:
    mine = seeded["first"]["event_id"]
    found = await query(app, asgi_client, fake_issuer, TEAM_SCOPE, path=f"{EVENTS}/{mine}")
    assert found.status_code == 200, found.text
    assert found.json()["event_id"] == mine
    assert found.json()["aggregate_id"] == "c-1"

    invisible = await query(
        app,
        asgi_client,
        fake_issuer,
        TEAM_SCOPE,
        path=f"{EVENTS}/{seeded['other_org']['event_id']}",
    )
    unknown = await query(
        app, asgi_client, fake_issuer, TEAM_SCOPE, path=f"{EVENTS}/{uuid.uuid4()}"
    )
    empty_scope = await query(app, asgi_client, fake_issuer, scope(), path=f"{EVENTS}/{mine}")
    bodies = []
    for response in (invisible, unknown, empty_scope):
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        body.pop("instance", None)
        body.pop("request_id", None)
        bodies.append(body)
    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]["type"] == "urn:dc:problem:not-found"

    malformed = await query(app, asgi_client, fake_issuer, TEAM_SCOPE, path=f"{EVENTS}/not-a-uuid")
    assert malformed.status_code == 422


async def test_the_internal_read_returns_the_shared_dto_with_category_and_detail(
    app: Any, asgi_client: Any, fake_issuer: FakeIssuer, mixed: dict[str, dict[str, Any]]
) -> None:
    from dc_audit.dto import AuditEventOut

    with respx.mock() as router:
        router.post(CHECK).respond(json={"outcome": "allow", "folder": None})
        async with asgi_client(app) as client:
            response = await client.get(
                "/internal/v1/events", params={"category": "access"}, headers=auth(fake_issuer)
            )
    assert response.status_code == 200
    items = response.json()
    assert {i["event_id"] for i in items} == {
        mixed["pipeline_read"]["event_id"],
        mixed["author_read"]["event_id"],
    }
    assert set(items[0]) == set(AuditEventOut.model_fields)
    assert items[0]["category"] == "access"
