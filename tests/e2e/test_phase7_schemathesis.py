"""Schemathesis conformance of the merged API document on the live stack (D-180, PLAT-06).

The merged document the engineers open (``https://traefik/api/v1/openapi.json``, served by
``api-docs``) is fetched inside the e2e session and handed to Schemathesis through its
fixture-based loader, so host-side collection (``pytest -m unit`` over ``tests/``) never
touches the network. Every generated case is sent through the gateway as the Global admin and
checked with the four conformance checks below.

Every operation of the merged document is included except the ones in ``EXCLUDED`` (each with
its reason). No operation is excluded to hide a failure: a conformance failure is fixed at its
source (the central post-processor ``dc_core.openapi`` or the owning route's ``responses=``).
Destructive generated calls land in a per-run Organization: every ``folder_id`` path parameter
and every ``parent_id`` body field is replaced with that Organization's id.

Bounds (research Pattern 7): about five fuzzed examples per operation and at most 30 requests
per second, below the gateway's ``rl-default`` limiter (average 50/s, burst 100).

Fuzzing traffic is not evidence that an operation is exercised by a journey, so these requests
are not counted by the operation-coverage registry (Plan 10).
"""

from __future__ import annotations

import os
import ssl
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
import schemathesis
from schemathesis.checks import not_a_server_error
from schemathesis.config import SchemathesisConfig
from schemathesis.specs.openapi.checks import (
    content_type_conformance,
    response_schema_conformance,
    status_code_conformance,
)

pytestmark = pytest.mark.e2e

MERGED_DOCUMENT = "/api/v1/openapi.json"
FOLDERS = "/api/v1/identity/folders"
MAX_EXAMPLES = 5
RATE_LIMIT = "30/s"
CHECKS = [
    not_a_server_error,
    status_code_conformance,
    content_type_conformance,
    response_schema_conformance,
]


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set; e2e tests run through scripts/e2e.sh")
    return value


@pytest.fixture(scope="module")
def merged_schema() -> Any:
    """The merged document as a Schemathesis schema, bounded and verifying TLS with the dev CA."""
    gateway_url = _env("DC_E2E_GATEWAY_URL").rstrip("/")
    ca_file = _env("DC_E2E_CA_FILE")
    with httpx.Client(
        verify=ssl.create_default_context(cafile=ca_file), timeout=httpx.Timeout(15.0)
    ) as client:
        response = client.get(f"{gateway_url}{MERGED_DOCUMENT}")
    assert response.status_code == 200, response.text
    config = SchemathesisConfig.from_dict(
        {
            "base-url": gateway_url,
            "tls-verify": ca_file,
            "rate-limit": RATE_LIMIT,
            "request-timeout": 15,
            "generation": {"max-examples": MAX_EXAMPLES, "database": "none"},
        }
    )
    return schemathesis.openapi.from_dict(response.json(), config=config)


# (method or None for every method, path regex, reason). The only exclusions (orchestrator
# decision, T-7-28); nothing else may be added here to make the run pass.
EXCLUDED: list[tuple[str | None, str, str]] = [
    (
        None,
        r"^/api/v1/identity/auth/",
        "login, refresh, logout and password rotation: a generated valid body would rotate the"
        " admin password (the admin JWT holds password:rotate) and feed the login throttle,"
        " breaking every later e2e fixture",
    ),
    (
        "POST",
        r"^/api/v1/identity/users$",
        "local users are permanent (deactivate, never delete), so generated users would pollute"
        " the shared stack",
    ),
]
RUN_ORG_PARAMETERS = ("folder_id", "parent_id")

schema = schemathesis.pytest.from_fixture("merged_schema")
for _method, _path_regex, _reason in EXCLUDED:
    schema = schema.exclude(method=_method, path_regex=_path_regex)


@pytest_asyncio.fixture
async def run_org(gateway: httpx.AsyncClient, admin_token: str) -> AsyncIterator[str]:
    """A fresh Organization under ROOT that every destructive generated call targets."""
    headers = {"Authorization": f"Bearer {admin_token}"}
    roots = await gateway.get(FOLDERS, params={"limit": 1}, headers=headers)
    assert roots.status_code == 200, roots.text
    root = roots.json()["items"][0]
    assert root["kind"] == "root"
    run = uuid.uuid4().hex[:10]
    created = await gateway.post(
        FOLDERS,
        json={"parent_id": root["id"], "slug": f"p7st-{run}", "display_name": f"P7 fuzz {run}"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    yield str(created.json()["id"])


def _target_run_org(case: Any, org_id: str) -> None:
    for name in RUN_ORG_PARAMETERS:
        if case.path_parameters and name in case.path_parameters:
            case.path_parameters[name] = org_id
    if isinstance(case.body, dict) and "parent_id" in case.body:
        case.body["parent_id"] = org_id


@schema.parametrize()
def test_the_live_api_answers_what_the_merged_document_says(
    case: Any, admin_token: str, run_org: str
) -> None:
    _target_run_org(case, run_org)
    case.call_and_validate(
        base_url=_env("DC_E2E_GATEWAY_URL").rstrip("/"),
        headers={"Authorization": f"Bearer {admin_token}"},
        checks=CHECKS,
    )
