"""AUD-02 on the real stack: audit events survive an audit-service outage and a NATS outage.

Three scenarios share one module so the ordering is explicit, and every stopped service is started
again in a ``finally`` block: a failing scenario never leaves the stack degraded.

* ``audit_outage``: audit is down while folders and a contract are written; the events wait in
  JetStream and each shows up in the audit database exactly once after audit restarts.
* ``nats_outage``: NATS is down while a folder is written; the outbox row stays unpublished; after
  NATS restarts the relay publishes it and audit stores it exactly once.
* ``no_duplicates``: restarting the producers afterwards (relay restarts, republish attempts) never
  creates a second audit row for any event written above.
* ``access_best_effort`` (AUD-04, D-185): a consumer's Latest fetch answers 200 with the content
  while NATS is down; after NATS is back, a new fetch produces its access event. Reads made during
  the outage may or may not be recorded, and nothing is asserted about them.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.e2e

FOLDERS = "/api/v1/identity/folders"
AUDIT_WINDOW_S = 60.0
NATS_WINDOW_S = 90.0
SETTLE_S = 10.0

# (kind of write -> folder or contract id) of everything the scenarios wrote, in order.
_written: dict[str, str] = {}


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _folder(gateway: httpx.AsyncClient, token: str, parent: str, slug: str) -> str:
    response = await gateway.post(
        FOLDERS,
        json={"parent_id": parent, "slug": slug, "display_name": slug},
        headers=_headers(token),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _root_id(gateway: httpx.AsyncClient, token: str) -> str:
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=_headers(token))
    assert roots.status_code == 200, roots.text
    return str(roots.json()["items"][0]["id"])


def _contract_yaml(odcs_id: str) -> bytes:
    return f"""kind: DataContract
apiVersion: v3.2.0
id: {odcs_id}
name: Audit Resilience
version: "1.0.0"
status: active
schema:
  - name: events
    physicalType: table
    properties:
      - name: event_id
        logicalType: string
""".encode()


@contextlib.contextmanager
def _stopped(compose: Any, service: str) -> Iterator[None]:
    """Stop ``service``; whatever happens inside, start it again afterwards."""
    compose.stop(service)
    try:
        yield
    finally:
        compose.start(service)


def _outbox_count(compose: Any, folder_id: str, *, published: bool) -> str:
    """Identity outbox rows for one folder id (UUID-parsed: nothing else is interpolated)."""
    aggregate_id = uuid.UUID(folder_id)
    state = "IS NOT NULL" if published else "IS NULL"
    return _psql(
        compose,
        "identity",
        "SELECT count(*) FROM outbox "  # noqa: S608 - UUID-validated value, test-only
        f"WHERE aggregate_id = '{aggregate_id}' AND action = 'folder.created' "
        f"AND published_at {state}",
    )


def _psql(compose: Any, database: str, query: str) -> str:
    container = compose.container("postgres")
    result = container.exec_run(["psql", "-U", "postgres", "-d", database, "-tAc", query])
    assert result.exit_code == 0, result.output.decode()
    return str(result.output.decode().strip())


async def _audit_events(
    internal: Callable[[str], httpx.AsyncClient], token: str, aggregate_id: str
) -> list[dict[str, Any]]:
    response = await internal("audit").get(
        "/internal/v1/events", params={"aggregate_id": aggregate_id}, headers=_headers(token)
    )
    response.raise_for_status()
    events: list[dict[str, Any]] = response.json()
    return events


async def _exactly_one(
    internal: Callable[[str], httpx.AsyncClient],
    token: str,
    aggregate_id: str,
    action: str,
) -> list[dict[str, Any]] | None:
    events = await _audit_events(internal, token, aggregate_id)
    matching = [event for event in events if event["action"] == action]
    return matching or None


async def test_audit_outage_events_are_delivered_once_after_audit_restarts(
    gateway: httpx.AsyncClient,
    internal: Callable[[str], httpx.AsyncClient],
    compose: Any,
    admin_token: str,
    wait_for: Callable[..., Any],
) -> None:
    root = await _root_id(gateway, admin_token)
    run = uuid.uuid4().hex[:10]
    with _stopped(compose, "audit"):
        org = await _folder(gateway, admin_token, root, f"ar-{run}")
        project = await _folder(gateway, admin_token, org, "payments")
        team = await _folder(gateway, admin_token, project, "ingest")
        created = await gateway.post(
            f"/api/v1/contracts/folders/{team}/contracts",
            content=_contract_yaml(f"urn:datacontract:e2e:{uuid.uuid4()}"),
            headers={**_headers(admin_token), "Content-Type": "application/yaml"},
        )
        assert created.status_code == 201, created.text
        contract = str(created.json()["contract"]["id"])
    _written.update({"org": org, "project": project, "team": team, "contract": contract})

    # Audit is back (the finally block started it); its healthcheck passing is not awaited by
    # `start`, so the polling below absorbs the restart.
    expected = {
        org: "folder.created",
        project: "folder.created",
        team: "folder.created",
        contract: "contract.created",
    }
    for aggregate_id, action in expected.items():
        events = await wait_for(
            lambda aggregate_id=aggregate_id, action=action: _exactly_one(
                internal, admin_token, aggregate_id, action
            ),
            AUDIT_WINDOW_S,
            description=f"{action} for {aggregate_id} after the audit restart",
        )
        assert len(events) == 1, events


async def test_nats_outage_events_wait_in_the_outbox_then_are_published_once(
    gateway: httpx.AsyncClient,
    internal: Callable[[str], httpx.AsyncClient],
    compose: Any,
    admin_token: str,
    wait_for: Callable[..., Any],
) -> None:
    root = await _root_id(gateway, admin_token)
    run = uuid.uuid4().hex[:10]
    with _stopped(compose, "nats"):
        org = await _folder(gateway, admin_token, root, f"nr-{run}")
        # The write succeeded without a broker, and its event is parked, not lost.
        assert _outbox_count(compose, org, published=False) == "1"
        # Nothing reaches audit while the broker is down.
        assert await _audit_events(internal, admin_token, org) == []
    _written["nats_org"] = org

    events = await wait_for(
        lambda: _exactly_one(internal, admin_token, org, "folder.created"),
        NATS_WINDOW_S,
        description=f"folder.created for {org} after the NATS restart",
    )
    assert len(events) == 1, events
    assert _outbox_count(compose, org, published=True) == "1"


async def test_no_duplicates_after_the_producers_restart(
    internal: Callable[[str], httpx.AsyncClient],
    compose: Any,
    admin_token: str,
    wait_for: Callable[..., Any],
) -> None:
    assert {"org", "project", "team", "contract", "nats_org"} <= set(_written), (
        "the outage scenarios above must run first (they populate the ids checked here)"
    )
    for service in ("identity", "contracts"):
        compose.container(service).restart(timeout=10)
    for service in ("identity", "contracts"):
        await wait_for(
            lambda service=service: compose.health(service) == "healthy",
            90.0,
            description=f"{service} healthy after restart",
        )
    await asyncio.sleep(SETTLE_S)  # the explicit settle window: a relay replay would land here
    for aggregate_id in _written.values():
        events = await _audit_events(internal, admin_token, aggregate_id)
        assert len(events) == 1, (aggregate_id, [event["action"] for event in events])


def _publishable_yaml(odcs_id: str) -> bytes:
    return f"""kind: DataContract
id: {odcs_id}
apiVersion: v3.2.0
name: Access Best Effort
version: "1.0.0"
status: active
domain: resilience
tags: [phase7, access]
description:
  purpose: Reads never wait for the broker.
schema:
  - name: events
    physicalType: table
    properties:
      - name: event_id
        logicalType: string
""".encode()


async def _access_rows(
    internal: Callable[[str], httpx.AsyncClient], token: str, contract_id: str
) -> list[dict[str, Any]]:
    response = await internal("audit").get(
        "/api/v1/audit/events",
        params={"category": "access", "aggregate_id": contract_id, "limit": 200},
        headers=_headers(token),
    )
    response.raise_for_status()
    rows: list[dict[str, Any]] = response.json()["items"]
    return rows


async def test_access_best_effort_reads_answer_during_a_nats_outage_and_are_recorded_after(
    gateway: httpx.AsyncClient,
    internal: Callable[[str], httpx.AsyncClient],
    compose: Any,
    admin_token: str,
    users: Any,
    wait_for: Callable[..., Any],
) -> None:
    root = await _root_id(gateway, admin_token)
    run = uuid.uuid4().hex[:10]
    org = await _folder(gateway, admin_token, root, f"ab-{run}")
    project = await _folder(gateway, admin_token, org, "payments")
    team = await _folder(gateway, admin_token, project, "ingest")
    raw = _publishable_yaml(f"urn:datacontract:e2e:{uuid.uuid4()}")
    created = await gateway.post(
        f"/api/v1/contracts/folders/{team}/contracts",
        content=raw,
        headers={**_headers(admin_token), "Content-Type": "application/yaml"},
    )
    assert created.status_code == 201, created.text
    contract = str(created.json()["contract"]["id"])
    draft = str(created.json()["draft"]["id"])
    published = await gateway.post(
        f"/api/v1/contracts/contracts/{contract}/drafts/{draft}/publish",
        headers={**_headers(admin_token), "If-Match": '"r1"'},
    )
    assert published.status_code == 201, published.text
    consumer = await users.create("abcs")
    granted = await gateway.post(
        f"{FOLDERS}/{team}/grants",
        json={"principal_type": "user", "principal_id": consumer.principal_id, "role": "consumer"},
        headers=_headers(admin_token),
    )
    assert granted.status_code == 201, granted.text
    consumer_headers = _headers(await users.token(consumer))
    latest = f"/api/v1/contracts/contracts/{contract}/versions/latest"

    with _stopped(compose, "nats"):
        during = await gateway.get(latest, headers=consumer_headers)
        # The read never waits for the broker: the content is served while NATS is down.
        assert during.status_code == 200, during.text
        assert during.content == raw

    await wait_for(
        lambda: compose.health("nats") == "healthy",
        NATS_WINDOW_S,
        description="nats healthy after the restart",
    )
    after_ids: set[str] = set()

    async def recorded_after_restart() -> list[dict[str, Any]] | None:
        request_id = str(uuid.uuid4())
        after_ids.add(request_id)
        fetched = await gateway.get(
            latest, headers={**consumer_headers, "X-Request-ID": request_id}
        )
        assert fetched.status_code == 200, fetched.text
        rows = await _access_rows(internal, admin_token, contract)
        matching = [row for row in rows if row["request_id"] in after_ids]
        return matching or None

    # Only reads made after NATS is back are required to appear; outage reads are not asserted.
    rows = await wait_for(
        recorded_after_restart,
        AUDIT_WINDOW_S,
        interval_s=2.0,
        description=f"an access event for {contract} after the NATS restart",
    )
    for row in rows:
        assert row["action"] == "read", row
        assert row["category"] == "access", row
        assert row["actor"]["id"] == consumer.principal_id, row
        assert row["detail"]["ref"] == "latest", row
