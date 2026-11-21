"""The real-stack world the integration tests read from (D-41).

Every test in this directory is skipped unless ``CONTRACTUM_IT_URL`` is set, so ``pytest`` exits 0
without a stack. With a stack up (the compose test profile, see ``scripts/skeleton-smoke.sh``) the
session creates one per-run world through plain httpx, as the Global admin. That is test
scaffolding, outside the product fence: the SDK under test only ever reads.

Environment (all read here, never printed):

* ``CONTRACTUM_IT_URL``: base URL of the gateway, for example ``https://localhost:8443``.
* ``CONTRACTUM_IT_CA``: the CA certificate the gateway's certificate chains to.
* ``CONTRACTUM_IT_ADMIN_PASSWORD_FILE``: the file holding the admin's rotated password.

The world, per run (unique ``sdk-it-<uuid8>`` names, so a warm stack can run it again):

* an Organization, a Project and a Team;
* an author (``team_editor``) who creates and publishes the contract;
* a reader (``consumer``) with a read-only personal access token;
* a service account in a group granted ``consumer``, holding one key;
* the mock Entra ``pipeline`` service principal, registered and granted ``consumer``;
* the contract ``<run>.orders`` with 1.0.0 (deprecated), 1.1.0 (Latest) and 2.0.0 (Preview, with
  a tighter ``amount`` range than 1.1.0).

The helper code is copied from ``tests/e2e/conftest.py``; nothing here imports it.
"""

from __future__ import annotations

import os
import secrets
import ssl
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from support.integration import CONTRACTS, FOLDERS, IDENTITY, Credentials, World

_HERE = Path(__file__).parent
ENV_URL = "CONTRACTUM_IT_URL"
ENV_CA = "CONTRACTUM_IT_CA"
ENV_ADMIN_PASSWORD_FILE = "CONTRACTUM_IT_ADMIN_PASSWORD_FILE"

LOGIN_PATH = f"{IDENTITY}/auth/login"
YAML = "application/yaml"
# The Entra tenant and personas seeded by infra/mock-oidc/config.json (the run-stack skill).
ENTRA_TENANT = "11111111-2222-4333-8444-555555555555"
PIPELINE_OID = "5e2b1ce0-0000-4000-8000-000000000004"
SUNSET_DAYS = 30


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Collect every integration test, then skip them all when no stack is configured."""
    if os.environ.get(ENV_URL):
        return
    skip = pytest.mark.skip(reason=f"{ENV_URL} is not set: no Contractum stack to test against")
    for item in items:
        if _HERE in Path(str(item.path)).parents:
            item.add_marker(skip)


@dataclass(frozen=True)
class ItSettings:
    """Where the stack is and how to trust it."""

    url: str
    ca: Path
    admin_password_file: Path


def contract_yaml(odcs_id: str, version: str, *, extra: str = "", amount: str = "") -> bytes:
    """A valid ODCS contract for the run; ``extra`` adds properties, ``amount`` its options."""
    text = f"""kind: DataContract
id: {odcs_id}
apiVersion: v3.2.0
name: SDK IT Orders
version: "{version}"
status: active
domain: sdk-it
description:
  purpose: Real-stack journey of the Contractum SDK.
schema:
  - name: orders
    physicalType: table
    properties:
      - name: order_id
        logicalType: string
        required: true
        unique: true
      - name: amount
        logicalType: integer{amount}
{extra}"""
    return text.encode("utf-8")


NOTE_PROPERTY = """      - name: note
        logicalType: string
"""
AMOUNT_UP_TO_1000 = """
        logicalTypeOptions:
          minimum: 0
          maximum: 1000"""


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _expiry() -> str:
    return (datetime.now(UTC) + timedelta(days=SUNSET_DAYS)).isoformat()


def _ok(response: httpx.Response, *codes: int) -> Any:
    """Return the JSON body of a response with an expected status; never print a token."""
    if response.status_code not in codes:
        raise AssertionError(
            f"{response.request.method} {response.request.url.path} answered "
            f"{response.status_code}: {response.text[:300]}"
        )
    return response.json() if response.content else None


def _login(http: httpx.Client, username: str, password: str) -> str:
    for attempt in range(5):
        response = http.post(LOGIN_PATH, json={"username": username, "password": password})
        if response.status_code == 429 and attempt < 4:
            time.sleep(1.0)
            continue
        return str(_ok(response, 200)["access_token"])
    raise AssertionError("unreachable")  # pragma: no cover


@pytest.fixture(scope="session")
def it_settings() -> ItSettings:
    """The stack location from the environment (the tests are skipped when it is not set)."""
    return ItSettings(
        url=os.environ[ENV_URL].rstrip("/"),
        ca=Path(os.environ[ENV_CA]),
        admin_password_file=Path(os.environ[ENV_ADMIN_PASSWORD_FILE]),
    )


@pytest.fixture(scope="session")
def admin_http(it_settings: ItSettings) -> Iterator[httpx.Client]:
    """A plain httpx client signed in as the Global admin (test scaffolding only)."""
    context = ssl.create_default_context(cafile=str(it_settings.ca))
    password = it_settings.admin_password_file.read_text(encoding="utf-8").strip()
    with httpx.Client(
        base_url=it_settings.url, verify=context, timeout=httpx.Timeout(30.0)
    ) as client:
        token = _login(client, "admin", password)
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


def _create_folder(http: httpx.Client, parent_id: str, slug: str) -> str:
    created = http.post(
        FOLDERS, json={"parent_id": parent_id, "slug": slug, "display_name": f"SDK IT {slug}"}
    )
    return str(_ok(created, 201)["id"])


def _grant(http: httpx.Client, folder_id: str, kind: str, principal: str, role: str) -> None:
    body = {"principal_type": kind, "principal_id": principal, "role": role}
    _ok(http.post(f"{FOLDERS}/{folder_id}/grants", json=body), 201)


def _create_user(http: httpx.Client, prefix: str) -> tuple[Credentials, str]:
    username = f"{prefix}-{uuid.uuid4().hex[:8]}"
    password = secrets.token_urlsafe(24)
    body = {"username": username, "password": password, "display_name": username}
    created = _ok(http.post(f"{IDENTITY}/users", json=body), 201)
    return Credentials(username, password), str(created["principal_id"])


def _register_pipeline(http: httpx.Client, root_id: str) -> None:
    """Register the Entra service principal; a 409 means an earlier run did it already."""
    path = f"{IDENTITY}/entra/service-principals"
    created = http.post(path, json={"owner_folder_id": root_id, "object_id": PIPELINE_OID})
    if created.status_code not in (200, 201, 409):
        _ok(created, 201)


def _create_service_account_key(http: httpx.Client, root_id: str, team: str, run: str) -> str:
    account = _ok(
        http.post(
            f"{IDENTITY}/service-accounts",
            json={"owner_folder_id": root_id, "name": f"sdk-it-sa-{run}"},
        ),
        201,
    )
    group = _ok(
        http.post(
            f"{IDENTITY}/groups", json={"owner_folder_id": root_id, "name": f"sdk-it-readers-{run}"}
        ),
        201,
    )
    member = {"principal_id": account["principal_id"]}
    _ok(http.post(f"{IDENTITY}/groups/{group['id']}/members", json=member), 201)
    _grant(http, team, "local_group", str(group["id"]), "consumer")
    key = _ok(
        http.post(
            f"{IDENTITY}/service-accounts/{account['id']}/keys",
            json={"name": "sdk-it", "expires_at": _expiry()},
        ),
        201,
    )
    return str(key["token"])


class _Author:
    """The team editor who creates, publishes, promotes and deprecates the versions."""

    def __init__(self, http: httpx.Client, token: str, contract_id: str | None = None) -> None:
        self.http = http
        self.headers = _bearer(token)
        self.contract_id = contract_id

    def create(self, team: str, content: bytes) -> dict[str, Any]:
        response = self.http.post(
            f"/api/v1/contracts/folders/{team}/contracts",
            content=content,
            headers={**self.headers, "Content-Type": YAML},
        )
        body: dict[str, Any] = _ok(response, 201)
        self.contract_id = str(body["contract"]["id"])
        return body

    def publish(self, draft: dict[str, Any]) -> dict[str, Any]:
        response = self.http.post(
            f"{CONTRACTS}/{self.contract_id}/drafts/{draft['id']}/publish",
            headers={**self.headers, "If-Match": draft["etag"]},
        )
        published: dict[str, Any] = _ok(response, 201)
        return published

    def new_version(self, from_version: str, bump: str, expect: str, content: bytes) -> str:
        """Start a draft from a published version, save ``content`` and publish it."""
        started = self.http.post(
            f"{CONTRACTS}/{self.contract_id}/drafts",
            json={"from_version": from_version, "bump": bump},
            headers=self.headers,
        )
        draft = _ok(started, 201)["draft"]
        assert draft["declared_version"] == expect, draft
        saved = self.http.put(
            f"{CONTRACTS}/{self.contract_id}/drafts/{draft['id']}",
            content=content,
            headers={**self.headers, "Content-Type": YAML, "If-Match": draft["etag"]},
        )
        draft = _ok(saved, 200)["draft"]
        return str(self.publish(draft)["status"])

    def lifecycle(self, action: str, version: str, body: dict[str, Any] | None = None) -> None:
        response = self.http.post(
            f"{CONTRACTS}/{self.contract_id}/versions/{version}/{action}",
            json=body,
            headers=self.headers,
        )
        _ok(response, 200)


def _build_contract(http: httpx.Client, team: str, editor_token: str, odcs_id: str) -> str:
    """1.0.0 (Latest), 1.1.0 (promoted), 1.0.0 deprecated, then 2.0.0 as the Preview."""
    author = _Author(http, editor_token)
    created = author.create(team, contract_yaml(odcs_id, "1.0.0"))
    assert author.publish(created["draft"])["status"] == "latest"
    state = author.new_version(
        "1.0.0", "minor", "1.1.0", contract_yaml(odcs_id, "1.1.0", extra=NOTE_PROPERTY)
    )
    assert state == "preview", state
    author.lifecycle("promote", "1.1.0")
    sunset = (datetime.now(UTC) + timedelta(days=SUNSET_DAYS)).date().isoformat()
    author.lifecycle("deprecate", "1.0.0", {"sunset_date": sunset})
    state = author.new_version(
        "1.1.0",
        "major",
        "2.0.0",
        contract_yaml(odcs_id, "2.0.0", extra=NOTE_PROPERTY, amount=AMOUNT_UP_TO_1000),
    )
    assert state == "preview", state
    assert author.contract_id is not None
    return author.contract_id


def _cleanup(http: httpx.Client, it_settings: ItSettings, ids: dict[str, str]) -> None:
    """Best effort: soft-delete the contract, then delete the empty folders. Never raises."""
    try:
        password = it_settings.admin_password_file.read_text(encoding="utf-8").strip()
        http.headers["Authorization"] = f"Bearer {_login(http, 'admin', password)}"
        http.delete(f"{CONTRACTS}/{ids['contract']}")
        for name in ("team", "project", "org"):
            http.delete(f"{FOLDERS}/{ids[name]}")
    except (httpx.HTTPError, AssertionError, OSError, KeyError):  # pragma: no cover
        return


@pytest.fixture(scope="session")
def it_world(it_settings: ItSettings, admin_http: httpx.Client) -> Iterator[World]:
    """Create the per-run world once; remove what can be removed afterwards."""
    http = admin_http
    run = uuid.uuid4().hex[:8]
    root = _ok(http.get(FOLDERS, params={"limit": 1}), 200)["items"][0]
    assert root["kind"] == "root"
    org_slug = f"sdk-it-{run}"
    org = _create_folder(http, root["id"], org_slug)
    project = _create_folder(http, org, "payments")
    team = _create_folder(http, project, "ingest")

    editor, editor_principal = _create_user(http, "sdk-ed")
    _grant(http, team, "user", editor_principal, "team_editor")
    reader, reader_principal = _create_user(http, "sdk-rd")
    _grant(http, team, "user", reader_principal, "consumer")
    _register_pipeline(http, str(root["id"]))
    _grant(http, team, "service_principal", f"entra:{ENTRA_TENANT}:{PIPELINE_OID}", "consumer")
    sa_key = _create_service_account_key(http, str(root["id"]), team, run)

    odcs_id = f"sdk-it-{run}.orders"
    with httpx.Client(
        base_url=it_settings.url,
        verify=ssl.create_default_context(cafile=str(it_settings.ca)),
        timeout=httpx.Timeout(30.0),
    ) as plain:
        editor_token = _login(plain, editor.username, editor.password)
        contract_id = _build_contract(plain, team, editor_token, odcs_id)
        reader_token = _login(plain, reader.username, reader.password)
        pat = _ok(
            plain.post(
                f"{IDENTITY}/pats",
                json={"name": f"sdk-it-{run}", "expires_at": _expiry(), "read_only": True},
                headers=_bearer(reader_token),
            ),
            201,
        )
    assert pat["read_only"] is True
    yield World(
        url=it_settings.url,
        ca=it_settings.ca,
        odcs_id=odcs_id,
        contract_id=contract_id,
        folder_path=f"{org_slug}/payments/ingest",
        reader=reader,
        reader_pat=str(pat["token"]),
        sa_key=sa_key,
    )
    _cleanup(
        http, it_settings, {"contract": contract_id, "team": team, "project": project, "org": org}
    )


@pytest.fixture(scope="session")
def reader_pat(it_world: World) -> str:
    """The reader's read-only personal access token (``dcp_<public_id>_<secret>``)."""
    return it_world.reader_pat


@pytest.fixture(scope="session")
def sa_key(it_world: World) -> str:
    """The service account's key."""
    return it_world.sa_key


@pytest.fixture
def entra_token(it_settings: ItSettings, it_world: World) -> str:
    """A fresh client-credentials token for the registered Entra service principal.

    The mock selects the persona by the ``scope`` form field and builds ``iss`` from ``Host`` and
    ``X-Forwarded-Proto``; through the gateway both are the browser-visible origin the platform
    trusts, so no header is forged here.
    """
    context = ssl.create_default_context(cafile=str(it_settings.ca))
    with httpx.Client(verify=context, timeout=httpx.Timeout(30.0)) as client:
        response = client.post(
            f"{it_settings.url}/entra/token",
            data={
                "grant_type": "client_credentials",
                "client_id": "pipeline",
                "client_secret": "pipeline",
                "scope": "pipeline",
            },
        )
    return str(_ok(response, 200)["access_token"])
