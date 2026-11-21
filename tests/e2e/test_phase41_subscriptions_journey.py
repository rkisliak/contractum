"""Phase 04.1 end to end through the gateway (ROADMAP success criteria SC1-SC5).

"Consumer teams record exactly which contract versions they depend on through subscriber cards,
and contract owners can see who consumes each version and reach them before changing it."

Every call goes through https://traefik except the audit read, Mailpit and the webhook sink (the
backend network, like the other e2e tests). Notices travel the decided path: the lifecycle
operation writes an outbox event, the relay publishes it to NATS, the ``contracts-notices``
consumer materialises one notice per card, the delivery worker hands email to the mail service
(authorised for ``mail:send`` through the identity PDP with the notifier key) and signs webhooks
(Standard Webhooks). Notices and deliveries are asynchronous, so every assertion about them
polls with ``wait_for``.

Every test is re-runnable on a warm stack: unique ODCS ids, folder slugs, card names and email
addresses per run, and the fake Graph, Mailpit and the sink are reset in fixtures or ``finally``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from conftest import FakeGraph, Mailpit, WebhookSink

pytestmark = pytest.mark.e2e

# Mirrors conftest.ENTRA_TENANT / ENTRA_PERSONAS (tests/e2e has no package, so a test module
# does not import its conftest).
ENTRA_DATA_ENG_GROUP = "9a0f0000-0000-4000-8000-0000000000a1"
ENTRA_ALICE = "a11ce000-0000-4000-8000-000000000001"

IDENTITY = "/api/v1/identity"
FOLDERS = f"{IDENTITY}/folders"
CONTRACTS = "/api/v1/contracts/contracts"
CARDS = "/api/v1/contracts/cards"
YAML = "application/yaml"
NOTICE_WAIT_S = 60.0
SINK_URL = "http://webhook-sink:8080/hooks"

Headers = dict[str, str]


def _bearer(token: str) -> Headers:
    return {"Authorization": f"Bearer {token}"}


def _expiry(days: int = 30) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _sunset(days: int = 90) -> str:
    return (datetime.now(UTC).date() + timedelta(days=days)).isoformat()


def _problem_type(response: httpx.Response) -> str:
    assert response.headers["content-type"].startswith("application/problem+json"), (
        response.headers,
        response.text,
    )
    return str(response.json()["type"])


def _contract_yaml(odcs_id: str, name: str, version: str) -> bytes:
    text = f"""kind: DataContract
id: {odcs_id}
apiVersion: v3.2.0
name: {name}
version: "{version}"
status: draft
domain: subscriptions
tags: [phase41]
description:
  purpose: Phase 04.1 journey.
schema:
  - name: facts
    physicalType: table
    properties:
      - name: fact_id
        logicalType: string
"""
    return text.encode("utf-8")


async def _folder(gateway: httpx.AsyncClient, headers: Headers, parent_id: str, slug: str) -> str:
    response = await gateway.post(
        FOLDERS,
        json={"parent_id": parent_id, "slug": slug, "display_name": f"Phase 4.1 {slug}"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _chain(
    gateway: httpx.AsyncClient, headers: Headers, root_id: str, slug: str
) -> tuple[str, str]:
    """An organization with one project and one team: ``(organization_id, team_id)``."""
    org = await _folder(gateway, headers, root_id, slug)
    project = await _folder(gateway, headers, org, "data")
    team = await _folder(gateway, headers, project, "team")
    return org, team


async def _grant(
    gateway: httpx.AsyncClient,
    headers: Headers,
    folder_id: str,
    principal_type: str,
    principal_id: str,
    role: str,
) -> None:
    response = await gateway.post(
        f"{FOLDERS}/{folder_id}/grants",
        json={"principal_type": principal_type, "principal_id": principal_id, "role": role},
        headers=headers,
    )
    assert response.status_code in (200, 201, 409), response.text


async def _register(
    gateway: httpx.AsyncClient, headers: Headers, path: str, owner_folder_id: str, object_id: str
) -> None:
    """Register an Entra group; on 409 (an earlier run on this stack) it is already there."""
    created = await gateway.post(
        path, json={"owner_folder_id": owner_folder_id, "object_id": object_id}, headers=headers
    )
    assert created.status_code in (200, 201, 409), created.text


async def _publish_new(
    gateway: httpx.AsyncClient, headers: Headers, team_id: str, odcs_id: str, version: str
) -> str:
    """Create a contract declaring ``version`` and publish it (it becomes Latest)."""
    created = await gateway.post(
        f"/api/v1/contracts/folders/{team_id}/contracts",
        content=_contract_yaml(odcs_id, f"Facts {odcs_id[-8:]}", version),
        headers={**headers, "Content-Type": YAML},
    )
    assert created.status_code == 201, created.text
    contract_id = str(created.json()["contract"]["id"])
    draft = created.json()["draft"]
    published = await gateway.post(
        f"{CONTRACTS}/{contract_id}/drafts/{draft['id']}/publish",
        headers={**headers, "If-Match": draft["etag"]},
    )
    assert published.status_code == 201, published.text
    assert published.json()["semver"] == version
    return contract_id


async def _publish_next(
    gateway: httpx.AsyncClient,
    headers: Headers,
    contract_id: str,
    from_version: str,
    bump: str,
    expect_version: str,
) -> None:
    """Derive the next version from ``from_version`` and publish it."""
    started = await gateway.post(
        f"{CONTRACTS}/{contract_id}/drafts",
        json={"from_version": from_version, "bump": bump},
        headers=headers,
    )
    assert started.status_code == 201, started.text
    draft = started.json()["draft"]
    assert draft["declared_version"] == expect_version, draft
    published = await gateway.post(
        f"{CONTRACTS}/{contract_id}/drafts/{draft['id']}/publish",
        headers={**headers, "If-Match": draft["etag"]},
    )
    assert published.status_code == 201, published.text


async def _create_card(
    gateway: httpx.AsyncClient,
    headers: Headers,
    team_id: str,
    name: str,
    contacts: list[dict[str, str]],
    *,
    email_enabled: bool = False,
    description: str = "Consumes the journey contracts.",
) -> str:
    created = await gateway.post(
        f"/api/v1/contracts/folders/{team_id}/cards",
        json={
            "name": name,
            "description": description,
            "contacts": contacts,
            "email_enabled": email_enabled,
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _email_contacts(*addresses: str) -> list[dict[str, str]]:
    return [{"name": f"Contact {i}", "kind": "email", "value": a} for i, a in enumerate(addresses)]


async def _subscribe(
    gateway: httpx.AsyncClient, headers: Headers, card_id: str, contract_id: str, version: str
) -> httpx.Response:
    return await gateway.post(
        f"{CARDS}/{card_id}/subscriptions",
        json={"contract_id": contract_id, "version": version},
        headers=headers,
    )


async def _notices(
    gateway: httpx.AsyncClient, headers: Headers, card_id: str
) -> list[dict[str, Any]]:
    response = await gateway.get(
        f"{CARDS}/{card_id}/notices", params={"limit": 100}, headers=headers
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def _notice_of(
    gateway: httpx.AsyncClient,
    headers: Headers,
    card_id: str,
    event_type: str,
    *,
    deliveries: int | None = None,
) -> dict[str, Any] | None:
    """The card's newest notice of ``event_type`` (with ``deliveries`` rows when asked), if any."""
    for notice in await _notices(gateway, headers, card_id):
        if notice["event_type"] == event_type and (
            deliveries is None or len(notice["deliveries"]) == deliveries
        ):
            return notice
    return None


async def _audit_events(
    audit: httpx.AsyncClient, headers: Headers, aggregate_id: str, action: str
) -> list[dict[str, Any]] | None:
    response = await audit.get(
        "/internal/v1/events", params={"aggregate_id": aggregate_id}, headers=headers
    )
    response.raise_for_status()
    events: list[dict[str, Any]] = [e for e in response.json() if e["action"] == action]
    return events or None


def _verify_standard_webhook(request: dict[str, Any], secret: str) -> bool:
    """Standard Webhooks v1 with the stdlib only: HMAC-SHA256 over ``id.timestamp.body``."""
    headers = request["headers"]
    body = base64.b64decode(request["body_b64"])
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    entries = [
        e.removeprefix("v1,") for e in headers["webhook-signature"].split() if e.startswith("v1,")
    ]
    return any(hmac.compare_digest(expected, entry) for entry in entries)


async def _sink_requests(sink: WebhookSink, minimum: int) -> list[dict[str, Any]] | None:
    found = await sink.requests()
    return found if len(found) >= minimum else None


async def _root_id(gateway: httpx.AsyncClient, admin: Headers) -> str:
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=admin)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    assert root["kind"] == "root"
    return str(root["id"])


async def test_sc5_email_deprecation_reaches_mailpit_through_nats_and_the_mail_service(
    gateway: httpx.AsyncClient,
    internal: Callable[[str], httpx.AsyncClient],
    admin_token: str,
    users: Any,
    mailpit: Mailpit,
    wait_for: Callable[..., Any],
) -> None:
    admin = _bearer(admin_token)
    root_id = await _root_id(gateway, admin)
    run = uuid.uuid4().hex[:10]

    # The notifier (D-105) was provisioned by identity from the dev-init credential file.
    accounts = await gateway.get(
        f"{IDENTITY}/service-accounts", params={"limit": 100}, headers=admin
    )
    assert accounts.status_code == 200, accounts.text
    assert "contracts-notifier" in {a["name"] for a in accounts.json()["items"]}, accounts.text

    org_a, team_a = await _chain(gateway, admin, root_id, f"p41a-{run}")
    _, team_b = await _chain(gateway, admin, root_id, f"p41b-{run}")
    owner = await users.create("p41own")
    consumer = await users.create("p41con")
    await _grant(gateway, admin, team_a, "user", owner.principal_id, "team_admin")
    await _grant(gateway, admin, team_b, "user", consumer.principal_id, "consumer")
    await _grant(gateway, admin, team_a, "user", consumer.principal_id, "consumer")
    own = _bearer(await users.token(owner))
    con = _bearer(await users.token(consumer))

    odcs_id = f"urn:datacontract:phase41:{uuid.uuid4()}"
    contract_id = await _publish_new(gateway, own, team_a, odcs_id, "1.0.0")

    first, second = f"a-{run}@example.test", f"b-{run}@example.test"
    card_id = await _create_card(
        gateway, con, team_b, f"mail-{run}", _email_contacts(first, second), email_enabled=True
    )
    pinned = await _subscribe(gateway, con, card_id, contract_id, "1.0.0")
    assert pinned.status_code == 201, pinned.text

    deprecated = await gateway.post(
        f"{CONTRACTS}/{contract_id}/versions/1.0.0/deprecate",
        json={"sunset_date": _sunset()},
        headers=own,
    )
    assert deprecated.status_code == 200, deprecated.text

    async def both_addresses_mailed() -> dict[str, Any] | None:
        for message in await mailpit.search(f"to:{first}"):
            addresses = {to["Address"] for to in message["To"]}
            if {first, second} <= addresses:
                return message
        return None

    message = await wait_for(
        both_addresses_mailed, NOTICE_WAIT_S, description="the deprecation mail in Mailpit"
    )
    assert odcs_id in message["Subject"], message["Subject"]
    assert "deprecated" in message["Subject"], message["Subject"]
    assert len(await mailpit.search(f"to:{second}")) == 1

    delivered = await wait_for(
        lambda: _delivered_email(gateway, con, card_id),
        NOTICE_WAIT_S,
        description="the email delivery to be recorded as delivered",
    )
    assert delivered["attempts"] == 1, delivered

    events = await wait_for(
        lambda: _audit_events(internal("audit"), admin, contract_id, "contract.version_deprecated"),
        30.0,
        description=f"the contract.version_deprecated audit event for {contract_id}",
    )
    assert card_id in events[0]["after"]["notify_card_ids"], events
    assert org_a  # the owning organization exists (kept for readable failures)


async def _delivered_email(
    gateway: httpx.AsyncClient, headers: Headers, card_id: str
) -> dict[str, Any] | None:
    notice = await _notice_of(gateway, headers, card_id, "contract.version.deprecated")
    if notice is None:
        return None
    for delivery in notice["deliveries"]:
        if delivery["channel"] == "email" and delivery["status"] == "delivered":
            return dict(delivery)
    return None


# --- the SC1-SC4 world: two organizations, one card with three kinds of members ---------------


@dataclass
class World:
    """Org A owns ``orders`` (2.3.0 Latest, 3.0.0 Preview), org C owns ``customers`` 1.0.0, org B
    is the consuming team. ``card_id`` has two email contacts and three linked members."""

    admin: Headers
    run: str
    team_a: str
    team_b: str
    team_c: str
    owner: Headers
    creator: Headers
    creator_principal: str
    orders_id: str
    orders_odcs: str
    customers_id: str
    alice: Headers
    key: Headers
    key_principal: str
    limited: Headers
    group_id: str
    card_id: str
    contacts: list[str]


async def _world(
    gateway: httpx.AsyncClient,
    admin_token: str,
    users: Any,
    entra_token: Callable[[str], Awaitable[str]],
) -> World:
    admin = _bearer(admin_token)
    root_id = await _root_id(gateway, admin)
    run = uuid.uuid4().hex[:10]
    org_a, team_a = await _chain(gateway, admin, root_id, f"p41a-{run}")
    _, team_b = await _chain(gateway, admin, root_id, f"p41b-{run}")
    org_c, team_c = await _chain(gateway, admin, root_id, f"p41c-{run}")

    owner = await users.create("p41own")
    creator = await users.create("p41con")
    limited = await users.create("p41lim")
    await _grant(gateway, admin, team_a, "user", owner.principal_id, "team_admin")
    await _grant(gateway, admin, team_b, "user", creator.principal_id, "consumer")
    for org in (org_a, org_c):
        await _grant(gateway, admin, org, "user", creator.principal_id, "consumer")
    await _grant(gateway, admin, org_a, "user", limited.principal_id, "consumer")
    own = _bearer(await users.token(owner))
    con = _bearer(await users.token(creator))
    lim = _bearer(await users.token(limited))

    orders_odcs = f"urn:datacontract:phase41:orders:{uuid.uuid4()}"
    orders_id = await _publish_new(gateway, own, team_a, orders_odcs, "2.3.0")
    await _publish_next(gateway, own, orders_id, "2.3.0", "major", "3.0.0")  # a Preview
    customers_id = await _publish_new(
        gateway, admin, team_c, f"urn:datacontract:phase41:customers:{uuid.uuid4()}", "1.0.0"
    )

    # The registered Entra group data-eng (alice is a member) and a ROOT-owned service account
    # with a key, both readers of the two contracts' organizations.
    await _register(gateway, admin, f"{IDENTITY}/entra/groups", root_id, ENTRA_DATA_ENG_GROUP)
    account = await gateway.post(
        f"{IDENTITY}/service-accounts",
        json={"owner_folder_id": root_id, "name": f"p41-sa-{run}"},
        headers=admin,
    )
    assert account.status_code == 201, account.text
    key = await gateway.post(
        f"{IDENTITY}/service-accounts/{account.json()['id']}/keys",
        json={"name": "key-1", "expires_at": _expiry()},
        headers=admin,
    )
    assert key.status_code == 201, key.text
    group = await gateway.post(
        f"{IDENTITY}/groups",
        json={"owner_folder_id": root_id, "name": f"p41-grp-{run}"},
        headers=admin,
    )
    assert group.status_code == 201, group.text
    group_id = str(group.json()["id"])
    added = await gateway.post(
        f"{IDENTITY}/groups/{group_id}/members",
        json={"principal_id": limited.principal_id},
        headers=admin,
    )
    assert added.status_code == 201, added.text
    sa_principal = str(account.json()["principal_id"])
    for org in (org_a, org_c):
        await _grant(gateway, admin, org, "entra_group", ENTRA_DATA_ENG_GROUP, "consumer")
        await _grant(gateway, admin, org, "service_account", sa_principal, "consumer")

    contacts = [f"first-{run}@example.test", f"second-{run}@example.test"]
    card_id = await _create_card(
        gateway, con, team_b, f"orders-consumers-{run}", _email_contacts(*contacts)
    )
    for member_type, member_id in (
        ("entra_group", ENTRA_DATA_ENG_GROUP),
        ("local_group", group_id),
        ("service_account", sa_principal),
    ):
        linked = await gateway.post(
            f"{CARDS}/{card_id}/members",
            json={"member_type": member_type, "member_id": member_id},
            headers=con,
        )
        assert linked.status_code == 201, linked.text
    return World(
        admin=admin,
        run=run,
        team_a=team_a,
        team_b=team_b,
        team_c=team_c,
        owner=own,
        creator=con,
        creator_principal=creator.principal_id,
        orders_id=orders_id,
        orders_odcs=orders_odcs,
        customers_id=customers_id,
        alice=_bearer(await entra_token("alice")),
        key=_bearer(str(key.json()["token"])),
        key_principal=sa_principal,
        limited=lim,
        group_id=group_id,
        card_id=card_id,
        contacts=contacts,
    )


async def _active_pin(
    gateway: httpx.AsyncClient, headers: Headers, card_id: str, contract_id: str, semver: str
) -> dict[str, Any]:
    response = await gateway.get(
        f"{CARDS}/{card_id}/subscriptions", params={"state": "active"}, headers=headers
    )
    assert response.status_code == 200, response.text
    for pin in response.json()["items"]:
        if pin["contract_id"] == contract_id and pin["semver"] == semver:
            return dict(pin)
    raise AssertionError(f"no active pin of {semver} on {contract_id}: {response.text}")


async def test_sc1_a_consumer_creates_a_card_links_three_members_and_they_subscribe_on_its_behalf(
    gateway: httpx.AsyncClient,
    internal: Callable[[str], httpx.AsyncClient],
    admin_token: str,
    users: Any,
    entra_token: Callable[[str], Awaitable[str]],
    fake_graph: FakeGraph,
    wait_for: Callable[..., Any],
) -> None:
    await fake_graph.reset()
    world = await _world(gateway, admin_token, users, entra_token)
    card = await gateway.get(f"{CARDS}/{world.card_id}", headers=world.creator)
    assert card.status_code == 200, card.text
    body = card.json()
    assert body["description"]
    assert [c["value"] for c in body["contacts"]] == world.contacts
    assert {(m["member_type"], m["member_id"]) for m in body["members"]} == {
        ("entra_group", ENTRA_DATA_ENG_GROUP),
        ("local_group", world.group_id),
        ("service_account", world.key_principal),
    }

    # alice (a member through data-eng) and the service account's key subscribe on its behalf.
    alice_pin = await _subscribe(gateway, world.alice, world.card_id, world.orders_id, "2.3.0")
    assert alice_pin.status_code == 201, alice_pin.text
    assert alice_pin.json()["pinned_by"].endswith(f":{ENTRA_ALICE}")
    key_pin = await _subscribe(gateway, world.key, world.card_id, world.customers_id, "1.0.0")
    assert key_pin.status_code == 201, key_pin.text
    assert key_pin.json()["pinned_by"] == world.key_principal

    # Someone outside the members, with folder access elsewhere, cannot even see the card.
    outsider = await users.create("p41out")
    await _grant(gateway, world.admin, world.team_c, "user", outsider.principal_id, "consumer")
    out = _bearer(await users.token(outsider))
    refused = await _subscribe(gateway, out, world.card_id, world.orders_id, "2.3.0")
    assert refused.status_code == 404, refused.text
    assert (await gateway.get(f"{CARDS}/{world.card_id}", headers=out)).status_code == 404
    # bob is a registered nobody: contracts refuses a principal identity does not know.
    bob = _bearer(await entra_token("bob"))
    unknown = await _subscribe(gateway, bob, world.card_id, world.orders_id, "2.3.0")
    assert unknown.status_code == 403, unknown.text
    assert _problem_type(unknown) == "urn:dc:problem:principal-not-registered"

    # Every card change is in the audit service.
    audit, admin = internal("audit"), world.admin

    async def trail() -> bool:
        async def count(action: str) -> int:
            response = await audit.get(
                "/internal/v1/events", params={"aggregate_id": world.card_id}, headers=admin
            )
            response.raise_for_status()
            return sum(1 for e in response.json() if e["action"] == action)

        return (
            await count("card.created") == 1
            and await count("card.member_added") == 3
            and await count("card.subscribed") == 2
        )

    await wait_for(trail, 30.0, description="the card's audit trail")


async def test_sc2_the_card_keeps_its_full_pin_record_including_ended_subscriptions(
    gateway: httpx.AsyncClient,
    admin_token: str,
    users: Any,
    entra_token: Callable[[str], Awaitable[str]],
    fake_graph: FakeGraph,
) -> None:
    await fake_graph.reset()
    world = await _world(gateway, admin_token, users, entra_token)
    pinned = await _subscribe(gateway, world.alice, world.card_id, world.orders_id, "2.3.0")
    assert pinned.status_code == 201, pinned.text
    moved = await gateway.post(
        f"{CARDS}/{world.card_id}/subscriptions/{pinned.json()['id']}/move",
        json={"version": "3.0.0"},  # a Preview may be pinned
        headers=world.alice,
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["semver"] == "3.0.0"

    customers = await _subscribe(gateway, world.key, world.card_id, world.customers_id, "1.0.0")
    assert customers.status_code == 201, customers.text
    left = await gateway.post(
        f"{CARDS}/{world.card_id}/subscriptions/{customers.json()['id']}/unsubscribe",
        json={"note": "moved to the new feed"},
        headers=world.key,
    )
    assert left.status_code == 200, left.text
    again = await _subscribe(gateway, world.key, world.card_id, world.customers_id, "1.0.0")
    assert again.status_code == 201, again.text

    card = await gateway.get(f"{CARDS}/{world.card_id}", headers=world.creator)
    assert card.status_code == 200, card.text
    body = card.json()
    assert body["name"].startswith("orders-consumers-")
    assert body["description"]
    assert len(body["contacts"]) == 2
    assert len(body["members"]) == 3
    assert body["subscriptions_truncated"] is False
    pins = body["subscriptions"]
    assert len(pins) == 4, pins
    ended = {pin["end_reason"]: pin for pin in pins if pin["ended_at"] is not None}
    assert set(ended) == {"moved", "unsubscribed"}
    assert ended["moved"]["semver"] == "2.3.0"
    assert ended["moved"]["replaced_by_id"] == moved.json()["id"]
    assert ended["moved"]["ended_by"].endswith(f":{ENTRA_ALICE}")
    assert ended["unsubscribed"]["end_note"] == "moved to the new feed"
    assert ended["unsubscribed"]["ended_by"] == world.key_principal
    assert all(pin["pinned_by"] and pin["pinned_at"] for pin in pins)
    assert {pin["semver"] for pin in pins if pin["ended_at"] is None} == {"3.0.0", "1.0.0"}

    # The standalone record route returns the same four rows.
    listed = await gateway.get(f"{CARDS}/{world.card_id}/subscriptions", headers=world.creator)
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["items"]) == 4


async def test_sc3_the_owner_sees_who_consumes_each_version_and_access_limits_pinning(
    gateway: httpx.AsyncClient,
    admin_token: str,
    users: Any,
    entra_token: Callable[[str], Awaitable[str]],
    fake_graph: FakeGraph,
) -> None:
    await fake_graph.reset()
    world = await _world(gateway, admin_token, users, entra_token)
    for version in ("2.3.0", "3.0.0"):
        pin = await _subscribe(gateway, world.alice, world.card_id, world.orders_id, version)
        assert pin.status_code == 201, pin.text

    view = await gateway.get(f"{CONTRACTS}/{world.orders_id}/subscribers", headers=world.owner)
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["next_cursor"] is None
    by_version = {entry["semver"]: entry for entry in body["versions"]}
    assert set(by_version) == {"2.3.0", "3.0.0"}
    for entry in by_version.values():
        (card,) = entry["cards"]
        assert card["card_id"] == world.card_id
        assert [c["value"] for c in card["contacts"]] == world.contacts
        assert set(card) == {
            "card_id",
            "name",
            "description",
            "contacts",
            "pinned_at",
            "pinned_by",
            "subscription_id",
        }
    assert "members" not in view.text  # no members, channels or other pins of another team's card
    assert "customers" not in view.text

    # A Consumer cannot see who consumes a contract.
    consumer_view = await gateway.get(
        f"{CONTRACTS}/{world.orders_id}/subscribers", headers=world.creator
    )
    assert consumer_view.status_code == 404, consumer_view.text

    # A member who may read orders but not customers cannot pin customers, and sees why: nothing.
    allowed = await _subscribe(gateway, world.limited, world.card_id, world.orders_id, "2.3.0")
    assert allowed.status_code == 200, allowed.text  # the card already pins it: a no-op
    refused = await _subscribe(gateway, world.limited, world.card_id, world.customers_id, "1.0.0")
    assert refused.status_code == 404, refused.text


async def test_sc4_pinned_versions_are_protected_and_every_blocked_or_removed_pin_notifies_the_card(
    gateway: httpx.AsyncClient,
    admin_token: str,
    users: Any,
    entra_token: Callable[[str], Awaitable[str]],
    fake_graph: FakeGraph,
    wait_for: Callable[..., Any],
) -> None:
    await fake_graph.reset()
    world = await _world(gateway, admin_token, users, entra_token)
    pinned = await _subscribe(gateway, world.alice, world.card_id, world.orders_id, "3.0.0")
    assert pinned.status_code == 201, pinned.text
    base = f"{CONTRACTS}/{world.orders_id}"

    refused = await gateway.post(f"{base}/versions/3.0.0/unpublish", headers=world.owner)
    assert refused.status_code == 409, refused.text
    assert _problem_type(refused) == "urn:dc:problem:version-pinned"
    assert world.card_id in refused.text
    assert f"orders-consumers-{world.run}" in refused.text
    await wait_for(
        lambda: _notice_of(
            gateway, world.creator, world.card_id, "contract.version.unpublish_blocked"
        ),
        NOTICE_WAIT_S,
        description="the unpublish_blocked notice on the card",
    )

    removed = await gateway.post(
        f"{base}/subscriptions/{pinned.json()['id']}/remove",
        json={"reason": "the 3.0.0 preview was withdrawn"},
        headers=world.owner,
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["end_reason"] == "removed_by_admin"
    notice = await wait_for(
        lambda: _notice_of(gateway, world.creator, world.card_id, "card.subscription.removed"),
        NOTICE_WAIT_S,
        description="the card.subscription.removed notice on the card",
    )
    assert "the 3.0.0 preview was withdrawn" in json.dumps(notice["payload"])
    unpublished = await gateway.post(f"{base}/versions/3.0.0/unpublish", headers=world.owner)
    assert unpublished.status_code == 200, unpublished.text

    # Deprecating a pinned version is allowed and tells the pinned cards.
    other = await _create_card(
        gateway, world.creator, world.team_b, f"other-{world.run}", _email_contacts()
    )
    other_pin = await _subscribe(gateway, world.creator, other, world.orders_id, "2.3.0")
    assert other_pin.status_code == 201, other_pin.text
    deprecated = await gateway.post(
        f"{base}/versions/2.3.0/deprecate", json={"sunset_date": _sunset()}, headers=world.owner
    )
    assert deprecated.status_code == 200, deprecated.text
    await wait_for(
        lambda: _notice_of(gateway, world.creator, other, "contract.version.deprecated"),
        NOTICE_WAIT_S,
        description="the deprecated notice on the other card",
    )


async def test_sc5_three_delivery_choices_record_one_deprecation_each_and_deliver_per_channel(
    gateway: httpx.AsyncClient,
    admin_token: str,
    users: Any,
    mailpit: Mailpit,
    webhook_sink: WebhookSink,
    wait_for: Callable[..., Any],
    ensure_webhook_hosts: Callable[..., Awaitable[None]],
) -> None:
    admin = _bearer(admin_token)
    # The allow-list is mandatory: the sink's host is listed before a card is pointed at it.
    await ensure_webhook_hosts("webhook-sink")
    root_id = await _root_id(gateway, admin)
    run = uuid.uuid4().hex[:10]
    org_a, team_a = await _chain(gateway, admin, root_id, f"p41sa-{run}")
    _, team_b = await _chain(gateway, admin, root_id, f"p41sb-{run}")
    owner = await users.create("p41own")
    creator = await users.create("p41con")
    await _grant(gateway, admin, team_a, "user", owner.principal_id, "team_admin")
    await _grant(gateway, admin, team_b, "user", creator.principal_id, "consumer")
    await _grant(gateway, admin, org_a, "user", creator.principal_id, "consumer")
    own = _bearer(await users.token(owner))
    con = _bearer(await users.token(creator))
    odcs_id = f"urn:datacontract:phase41:feed:{uuid.uuid4()}"
    contract_id = await _publish_new(gateway, own, team_a, odcs_id, "1.0.0")

    addresses = [f"e1-{run}@example.test", f"e2-{run}@example.test"]
    email_card = await _create_card(
        gateway, con, team_b, f"email-{run}", _email_contacts(*addresses), email_enabled=True
    )
    webhook_card = await _create_card(
        gateway, con, team_b, f"hook-{run}", _email_contacts(f"w-{run}@example.test")
    )
    none_card = await _create_card(
        gateway, con, team_b, f"none-{run}", _email_contacts(f"n-{run}@example.test")
    )
    enabled = await gateway.patch(
        f"{CARDS}/{webhook_card}",
        json={"webhook_url": SINK_URL, "webhook_enabled": True},
        headers=con,
    )
    assert enabled.status_code == 200, enabled.text
    secret = str(enabled.json()["webhook_secret"])
    assert secret.startswith("whsec_")
    for card in (email_card, webhook_card, none_card):
        pin = await _subscribe(gateway, con, card, contract_id, "1.0.0")
        assert pin.status_code == 201, pin.text

    # The receiver fails its first two attempts; the owner is never slowed down by it.
    await webhook_sink.fail(2, 500)
    started = datetime.now(UTC)
    deprecated = await gateway.post(
        f"{CONTRACTS}/{contract_id}/versions/1.0.0/deprecate",
        json={"sunset_date": _sunset()},
        headers=own,
    )
    assert deprecated.status_code == 200, deprecated.text
    assert (datetime.now(UTC) - started).total_seconds() < 2.0

    # Email: one message to both contacts of the email card, none for the other two cards.
    async def mailed() -> dict[str, Any] | None:
        for message in await mailpit.search(f"to:{addresses[0]}"):
            if {to["Address"] for to in message["To"]} >= set(addresses):
                return message
        return None

    message = await wait_for(mailed, NOTICE_WAIT_S, description="the email in Mailpit")
    assert odcs_id in message["Subject"]
    assert await mailpit.search(f"to:w-{run}@example.test") == []
    assert await mailpit.search(f"to:n-{run}@example.test") == []

    # Webhook: three requests with one stable webhook-id, signed with the secret from enable time.
    async def delivered_webhook() -> dict[str, Any] | None:
        notice = await _notice_of(
            gateway, con, webhook_card, "contract.version.deprecated", deliveries=1
        )
        if notice and notice["deliveries"][0]["status"] == "delivered":
            return dict(notice["deliveries"][0])
        return None

    delivery = await wait_for(
        delivered_webhook, NOTICE_WAIT_S, description="the webhook delivery to be delivered"
    )
    assert delivery["channel"] == "webhook"
    assert delivery["attempts"] == 3, delivery
    assert delivery["last_status"] == 200, delivery
    received = await wait_for(
        lambda: _sink_requests(webhook_sink, 3), 10.0, description="three requests at the sink"
    )
    assert len(received) == 3, received
    assert {r["path"] for r in received} == {"/hooks"}
    assert len({r["headers"]["webhook-id"] for r in received}) == 1
    assert _verify_standard_webhook(received[-1], secret)
    assert not _verify_standard_webhook(
        received[-1], "whsec_" + base64.b64encode(b"x" * 32).decode()
    )

    # Email and none cards: the same deprecation is recorded on each, with the right deliveries.
    email_notice = await wait_for(
        lambda: _notice_of(gateway, con, email_card, "contract.version.deprecated", deliveries=1),
        NOTICE_WAIT_S,
        description="the email card's notice",
    )
    assert email_notice["deliveries"][0]["channel"] == "email"
    none_notice = await wait_for(
        lambda: _notice_of(gateway, con, none_card, "contract.version.deprecated", deliveries=0),
        NOTICE_WAIT_S,
        description="the none card's notice",
    )
    assert none_notice["deliveries"] == []
    assert none_notice["event_type"] == email_notice["event_type"]
