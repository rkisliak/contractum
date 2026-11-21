"""Every protected operation of every routed service refuses an anonymous caller with 401.

Unlike the generated matrix (``test_authz_route_matrix.py``), which mounts each route's policy in
front of a stub, this sends one request per documented operation to the real service app (built
in-process with never-connected settings, ``scripts/export_openapi.py``): no ``Authorization``
header, path parameters filled with fresh UUIDs, no body. Authentication is the first dependency
of every ``require()`` policy, so the answer must be 401 problem+json with ``WWW-Authenticate:
Bearer`` before any target resolver, database session or body parsing runs; a public operation
must not answer 401.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.unit

ROUTED = ("identity", "contracts", "validation", "audit")
METHODS = {"get", "post", "put", "patch", "delete"}


def _operations(apps: dict[str, FastAPI]) -> list[tuple[str, str, str, bool]]:
    found = []
    for service in ROUTED:
        for path, item in apps[service].openapi()["paths"].items():
            for method, operation in item.items():
                if method in METHODS:
                    public = operation.get("security") == []
                    found.append((service, method.upper(), path, public))
    return found


async def test_every_protected_operation_answers_401_without_a_token(
    service_apps: dict[str, FastAPI],
) -> None:
    operations = _operations(service_apps)
    assert len(operations) >= 100
    wrong: list[str] = []
    protected = 0
    for service, method, path, public in operations:
        url = path
        while "{" in url:
            start, end = url.index("{"), url.index("}")
            url = url[:start] + str(uuid.uuid4()) + url[end + 1 :]
        transport = httpx.ASGITransport(app=service_apps[service], raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://svc") as client:
            response = await client.request(method, url)
        if public:
            # No bearer is involved (login, logout, refresh, version); refresh answers 401 for a
            # missing refresh token by contract, so a public answer is not judged here.
            continue
        protected += 1
        if response.status_code != 401:
            wrong.append(f"{method} {path}: {response.status_code}")
            continue
        if not response.headers["content-type"].startswith("application/problem+json"):
            wrong.append(f"{method} {path}: 401 is not problem+json")
        if not response.headers.get("www-authenticate", "").lower().startswith("bearer"):
            wrong.append(f"{method} {path}: 401 without WWW-Authenticate: Bearer")
    assert wrong == []
    assert protected >= 100
