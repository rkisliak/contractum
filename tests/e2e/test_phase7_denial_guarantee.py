"""AUD-04 on the real stack: every denied permission check is recorded exactly once (D-184).

Each request carries its own UUID4 ``X-Request-ID``. Identity records it as the denial's
``request_id`` on its own routes and on its internal check route, and contracts forwards it
through ``RemoteAuthorizer``. So the audit query (``category=denial``, all pages) can be matched
against the exact set of ids the client sent.

All traffic runs on the backend network (``internal("identity")``, ``internal("contracts")``,
``internal("audit")``), never through Traefik: its rate limit would turn part of the burst into
429s, which never reach the PDP and are correctly not denials. Only plain ``require()`` routes
and identity's internal check are used. Routes that make in-handler probe checks (card members,
validation batch facts, contracts source checks, subscription reads) also record denials, and
would make "exactly N" depend on handler internals.

Request kinds (one denial each):

* ``identity_not_found``: ``GET /api/v1/identity/folders/{random}`` as a user with no grants
  (``folder:read``, 404, outcome ``not_found``);
* ``contracts_not_found``: ``GET /api/v1/contracts/folders/{random}/available-templates`` as the
  same user (``contract:create`` checked by identity through ``RemoteAuthorizer``, 404, outcome
  ``not_found``, service ``contracts`` and the route template);
* ``forbidden``: identity's ``POST /internal/v1/authz/check`` of the root permission
  ``user:read`` as that user (200 with outcome ``forbidden``);
* ``interactive``: a read-write PAT of a Team admin on ``POST .../folders/{team}/grants``
  (``grant:manage`` is interactive only: 403 ``interactive-session-required``).

The guarantee test follows the deterministic order of research Pattern 10: fire half, stop NATS,
fire the other half (the denials commit into identity's outbox), restart identity with the
backlog still in its outbox, start NATS, then assert. No request is in flight while identity
restarts, so "exactly N" is deterministic. Every stopped container is started again in a
``finally`` block, and the test waits for health before it ends.

Sizes: ``DC_E2E_DENIALS`` (default 2000) and ``DC_E2E_DENIAL_CONCURRENCY`` (default 25). An
empty value means the default. The concurrency default is sized to one identity worker: at 50 the
worker saturates, contracts' 2 s authz call times out and contracts fails closed with 503 (no
denial is lost; G-07-11-2, the same capacity limit as G-07-09-1).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.e2e

IDENTITY = "/api/v1/identity"
FOLDERS = f"{IDENTITY}/folders"
CHECK = "/internal/v1/authz/check"
AUDIT_EVENTS = "/api/v1/audit/events"
AVAILABLE_TEMPLATES = "/api/v1/contracts/folders/{folder_id}/available-templates"
DELIVERY_WINDOW_S = 180.0
HEALTH_WINDOW_S = 120.0
SETTLE_S = 5.0
PAGE_LIMIT = 200
INTERACTIVE_PROBLEM = "urn:dc:problem:interactive-session-required"

KINDS = ("identity_not_found", "contracts_not_found", "forbidden", "interactive")
EXPECTED_OUTCOME = {
    "identity_not_found": "not_found",
    "contracts_not_found": "not_found",
    "forbidden": "forbidden",
    "interactive": "interactive_required",
}
EXPECTED_ROUTE = {
    "identity_not_found": ("identity", f"{FOLDERS}/{{folder_id}}"),
    "contracts_not_found": ("contracts", AVAILABLE_TEMPLATES),
    "interactive": ("identity", f"{FOLDERS}/{{folder_id}}/grants"),
}


def _env_int(name: str, default: int) -> int:
    """An integer from the environment; unset or empty means ``default``."""
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass(frozen=True)
class Actors:
    """The per-run principals: a user with no grants and a Team admin's read-write PAT."""

    no_grant: dict[str, str]
    no_grant_principal: str
    pat: dict[str, str]
    team: str


@dataclass(frozen=True)
class Sent:
    kind: str
    status: int
    problem: str | None


async def _folder(
    identity: httpx.AsyncClient, admin: dict[str, str], parent: str, slug: str
) -> str:
    response = await identity.post(
        FOLDERS,
        json={"parent_id": parent, "slug": slug, "display_name": f"Phase 7 {slug}"},
        headers=admin,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _root_id(identity: httpx.AsyncClient, admin: dict[str, str]) -> str:
    roots = await identity.get(FOLDERS, params={"limit": 1}, headers=admin)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    assert root["kind"] == "root", root
    return str(root["id"])


async def _actors(
    internal: Callable[[str], httpx.AsyncClient], admin_token: str, users: Any, prefix: str
) -> Actors:
    identity = internal("identity")
    admin = _bearer(admin_token)
    run = uuid.uuid4().hex[:10]
    root = await _root_id(identity, admin)
    org = await _folder(identity, admin, root, f"{prefix}-{run}")
    project = await _folder(identity, admin, org, "payments")
    team = await _folder(identity, admin, project, "ingest")

    stranger = await users.create(f"{prefix}ng")
    team_admin = await users.create(f"{prefix}ta")
    granted = await identity.post(
        f"{FOLDERS}/{team}/grants",
        json={
            "principal_type": "user",
            "principal_id": team_admin.principal_id,
            "role": "team_admin",
        },
        headers=admin,
    )
    assert granted.status_code == 201, granted.text
    pat = await identity.post(
        f"{IDENTITY}/pats",
        json={
            "name": f"{prefix}-{run}",
            "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
        headers=_bearer(await users.token(team_admin)),
    )
    assert pat.status_code == 201, pat.text
    return Actors(
        no_grant=_bearer(await users.token(stranger)),
        no_grant_principal=stranger.principal_id,
        pat=_bearer(str(pat.json()["token"])),
        team=team,
    )


def _plan(count: int) -> list[str]:
    """``count`` request kinds, the four kinds in turn."""
    return [KINDS[i % len(KINDS)] for i in range(count)]


async def _send(
    internal: Callable[[str], httpx.AsyncClient], actors: Actors, kind: str, request_id: str
) -> httpx.Response:
    rid = {"X-Request-ID": request_id}
    if kind == "identity_not_found":
        return await internal("identity").get(
            f"{FOLDERS}/{uuid.uuid4()}", headers={**actors.no_grant, **rid}
        )
    if kind == "contracts_not_found":
        return await internal("contracts").get(
            AVAILABLE_TEMPLATES.format(folder_id=uuid.uuid4()), headers={**actors.no_grant, **rid}
        )
    if kind == "forbidden":
        return await internal("identity").post(
            CHECK, json={"permission": "user:read"}, headers={**actors.no_grant, **rid}
        )
    if kind == "interactive":
        return await internal("identity").post(
            f"{FOLDERS}/{actors.team}/grants",
            json={
                "principal_type": "user",
                "principal_id": actors.no_grant_principal,
                "role": "team_reader",
            },
            headers={**actors.pat, **rid},
        )
    raise AssertionError(f"unknown request kind {kind}")


def _problem(response: httpx.Response) -> str | None:
    if not response.headers.get("content-type", "").startswith("application/problem+json"):
        return None
    return str(response.json().get("type"))


def _answered_as_expected(kind: str, response: httpx.Response) -> bool:
    if kind in ("identity_not_found", "contracts_not_found"):
        return response.status_code == 404
    if kind == "forbidden":
        return response.status_code == 200 and response.json().get("outcome") == "forbidden"
    return response.status_code == 403 and _problem(response) == INTERACTIVE_PROBLEM


async def _fire(
    internal: Callable[[str], httpx.AsyncClient],
    actors: Actors,
    kinds: list[str],
    concurrency: int,
) -> dict[str, Sent]:
    """Send every request kind with a fresh UUID4 X-Request-ID over ``concurrency`` tasks.

    Returns ``request_id -> Sent``; asserts that every request answered its expected refusal.
    """
    queue: asyncio.Queue[str] = asyncio.Queue()
    for kind in kinds:
        queue.put_nowait(kind)
    sent: dict[str, Sent] = {}
    wrong: list[tuple[str, str, int, str]] = []

    async def worker() -> None:
        while True:
            try:
                kind = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            request_id = str(uuid.uuid4())
            response = await _send(internal, actors, kind, request_id)
            sent[request_id] = Sent(kind, response.status_code, _problem(response))
            if not _answered_as_expected(kind, response):
                wrong.append((kind, request_id, response.status_code, response.text[:300]))

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    assert not wrong, f"{len(wrong)} requests answered unexpectedly, first: {wrong[:5]}"
    assert len(sent) == len(kinds)
    return sent


async def _denials(
    audit: httpx.AsyncClient, admin: dict[str, str], since: datetime, wanted: set[str]
) -> list[dict[str, Any]]:
    """Every denial event since ``since`` whose request_id is in ``wanted`` (all pages)."""
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {
            "category": "denial",
            "from": since.isoformat(),
            "limit": PAGE_LIMIT,
        }
        if cursor is not None:
            params["cursor"] = cursor
        response = await audit.get(AUDIT_EVENTS, params=params, headers=admin)
        assert response.status_code == 200, response.text
        body = response.json()
        found.extend(item for item in body["items"] if item["request_id"] in wanted)
        cursor = body["next_cursor"]
        if cursor is None:
            return found


async def _assert_exactly_once(
    internal: Callable[[str], httpx.AsyncClient],
    admin: dict[str, str],
    since: datetime,
    sent: dict[str, Sent],
    wait_for: Callable[..., Any],
) -> list[dict[str, Any]]:
    """Wait until every sent request id has a denial, then assert set equality, no duplicates."""
    wanted = set(sent)

    async def complete() -> list[dict[str, Any]] | None:
        rows = await _denials(internal("audit"), admin, since, wanted)
        return rows if {row["request_id"] for row in rows} >= wanted else None

    await wait_for(
        complete,
        DELIVERY_WINDOW_S,
        interval_s=2.0,
        description=f"{len(wanted)} denial events by request id",
    )
    # A redelivery would land after the first complete read; read once more after a settle.
    await asyncio.sleep(SETTLE_S)
    rows = await _denials(internal("audit"), admin, since, wanted)
    counts = Counter(row["request_id"] for row in rows)
    duplicates = {rid: n for rid, n in counts.items() if n > 1}
    assert not duplicates, f"{len(duplicates)} request ids recorded more than once"
    assert set(counts) == wanted
    assert len(rows) == len(sent)
    for row in rows:
        kind = sent[row["request_id"]].kind
        assert row["category"] == "denial", row
        assert row["action"] == "check.denied", row
        assert row["detail"]["outcome"] == EXPECTED_OUTCOME[kind], (kind, row)
        if kind in EXPECTED_ROUTE:
            service, route = EXPECTED_ROUTE[kind]
            assert (row["detail"]["service"], row["detail"]["route"]) == (service, route), (
                kind,
                row,
            )
    return rows


async def test_tracer_fifty_denied_checks_are_each_recorded_once_by_request_id(
    internal: Callable[[str], httpx.AsyncClient],
    admin_token: str,
    users: Any,
    wait_for: Callable[..., Any],
) -> None:
    actors = await _actors(internal, admin_token, users, "p7dt")
    since = datetime.now(UTC) - timedelta(seconds=5)
    plan = _plan(50)
    sent = await _fire(internal, actors, plan, concurrency=10)
    assert Counter(s.kind for s in sent.values()) == Counter(plan)
    rows = await _assert_exactly_once(internal, _bearer(admin_token), since, sent, wait_for)
    assert len(rows) == 50


async def _healthy(compose: Any, service: str, wait_for: Callable[..., Any]) -> None:
    await wait_for(
        lambda: compose.health(service) == "healthy",
        HEALTH_WINDOW_S,
        interval_s=1.0,
        description=f"{service} healthy",
    )


async def _restore(compose: Any, wait_for: Callable[..., Any], *services: str) -> None:
    """Start every listed service that is not running, then wait until each is healthy."""
    for service in services:
        container = compose.container(service)
        container.reload()
        if container.status != "running":
            compose.start(service)
    for service in services:
        await _healthy(compose, service, wait_for)


async def test_exactly_n_denials_survive_a_nats_outage_and_an_identity_restart(
    internal: Callable[[str], httpx.AsyncClient],
    compose: Any,
    _admin_tokens: Any,
    users: Any,
    wait_for: Callable[..., Any],
) -> None:
    total = _env_int("DC_E2E_DENIALS", 2000)
    # 25, not 50: one identity worker saturates at 50 (G-07-11-2, same limit as G-07-09-1)
    concurrency = _env_int("DC_E2E_DENIAL_CONCURRENCY", 25)
    assert total >= 2, total
    actors = await _actors(internal, await _admin_tokens.get(), users, "p7dg")
    since = datetime.now(UTC) - timedelta(seconds=5)

    first_half = _plan(total // 2)
    second_half = _plan(total - total // 2)
    sent = await _fire(internal, actors, first_half, concurrency)
    try:
        compose.stop("nats")
        try:
            # The broker is down: every refusal is still answered, its row commits in the outbox.
            sent |= await _fire(internal, actors, second_half, concurrency)
            # Nothing is in flight now; the relay restarts with the backlog still unpublished.
            compose.container("identity").restart(timeout=10)
            await _healthy(compose, "identity", wait_for)
        finally:
            compose.start("nats")
    finally:
        await _restore(compose, wait_for, "nats", "identity")

    assert len(sent) == total
    rows = await _assert_exactly_once(
        internal, _bearer(await _admin_tokens.get()), since, sent, wait_for
    )
    assert len(rows) == total
