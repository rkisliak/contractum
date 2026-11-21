"""Operation coverage of the e2e suite (D-181 part 3): collected and run last in ``tests/e2e``.

The ``gateway`` and ``internal`` clients of ``conftest.py`` record the method and path of every
request answered below 400 in the session fixture ``operation_registry``, and of every other
request in ``operation_refusals`` (never a header, token, query string or body). Only the first is
coverage (WR-05): an operation whose only e2e traffic is refusals (401, a 404 probe, 422, 503)
is not exercised by any journey. One that is deliberately only ever refused belongs in
``operation_exemptions.json`` with a reason. This module matches those paths to the path
templates of the merged OpenAPI document (``GET /api/v1/openapi.json`` through the gateway):

* a ``{param}`` matches exactly one path segment and the pattern is anchored;
* when several templates match, the one with the most literal segments wins;
* the query string is ignored and an ``/internal`` path never matches (it is not in the document).

The report test always writes ``/dev-out/phase7-operation-coverage.json`` (the host's
``.dev/``): covered, uncovered, refused-only, exempt and stale-exemption operationIds and the
sizes. It never
fails. The gate test fails when an operationId is neither covered nor listed in
``operation_exemptions.json`` with a reason, but only when the session collected every
``tests/e2e/test_phase*.py`` module; a targeted run skips it. An exemption for an operation that
turns out to be covered is reported as stale, never a failure.

Schemathesis traffic is not coverage evidence: it uses its own transport, not these clients.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytestmark = pytest.mark.e2e

E2E_DIR = Path(__file__).resolve().parent
EXEMPTIONS_FILE = E2E_DIR / "operation_exemptions.json"
REPORT = Path("/dev-out/phase7-operation-coverage.json")
MERGED_DOCUMENT = "/api/v1/openapi.json"
METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
_PARAM = re.compile(r"^\{[^/{}]+\}$")


@dataclass(frozen=True)
class Template:
    method: str
    path: str
    operation_id: str
    pattern: re.Pattern[str]
    literals: int


def compile_templates(document: dict[str, Any]) -> list[Template]:
    """Every operation of an OpenAPI document as an anchored, one-segment-per-param pattern."""
    templates: list[Template] = []
    for path, item in document.get("paths", {}).items():
        segments = path.strip("/").split("/")
        parts = ["[^/]+" if _PARAM.match(s) else re.escape(s) for s in segments]
        pattern = re.compile("^/" + "/".join(parts) + "/?$")
        literals = sum(1 for s in segments if not _PARAM.match(s))
        for method, operation in item.items():
            if method in METHODS:
                templates.append(
                    Template(method.upper(), path, operation["operationId"], pattern, literals)
                )
    return templates


def match(templates: Iterable[Template], method: str, raw_path: str) -> str | None:
    """The operationId a request (method, raw path) exercised, or None."""
    path = raw_path.split("?", 1)[0]
    if path == "/internal" or path.startswith("/internal/"):
        return None
    candidates = [t for t in templates if t.method == method.upper() and t.pattern.match(path)]
    if not candidates:
        return None
    return max(candidates, key=lambda t: t.literals).operation_id


def covered_operations(
    templates: list[Template], registry: Iterable[tuple[str, str]]
) -> tuple[set[str], int]:
    """(covered operationIds, number of recorded requests that matched no operation)."""
    covered: set[str] = set()
    unmatched = 0
    for method, path in registry:
        found = match(templates, method, path)
        if found is None:
            unmatched += 1
        else:
            covered.add(found)
    return covered, unmatched


def _exemptions() -> dict[str, str]:
    data: dict[str, str] = json.loads(EXEMPTIONS_FILE.read_text(encoding="utf-8"))
    return data


def _all_journeys_collected(session: pytest.Session) -> tuple[bool, list[str]]:
    required = {p.name for p in E2E_DIR.glob("test_phase*.py")}
    collected = {Path(str(item.path)).name for item in session.items}
    missing = sorted(required - collected)
    return not missing, missing


@pytest_asyncio.fixture(scope="module")
async def merged(gateway: httpx.AsyncClient) -> dict[str, Any]:
    response = await gateway.get(MERGED_DOCUMENT)
    assert response.status_code == 200, response.text
    document: dict[str, Any] = response.json()
    return document


@pytest.fixture(scope="module")
def templates(merged: dict[str, Any]) -> list[Template]:
    found = compile_templates(merged)
    assert found, "the merged document has no operation"
    return found


# --- the matcher (pure; always runs) ----------------------------------------------------------


def test_a_parameter_matches_one_segment(templates: list[Template]) -> None:
    by_id = {t.operation_id: t.path for t in templates}
    found = match(templates, "GET", "/api/v1/contracts/contracts/abc/versions/1.0.0")
    assert found is not None
    assert by_id[found] == "/api/v1/contracts/contracts/{contract_id}/versions/{ref}"
    metadata = match(templates, "GET", "/api/v1/contracts/contracts/abc/versions/latest/metadata")
    assert metadata is not None
    assert by_id[metadata] == "/api/v1/contracts/contracts/{contract_id}/versions/{ref}/metadata"
    assert match(templates, "GET", "/api/v1/contracts/contracts/a/b/versions/1.0.0") is None


def test_a_literal_segment_beats_a_parameter() -> None:
    document = {
        "paths": {
            "/api/v1/x/{a}/{b}": {"get": {"operationId": "params"}},
            "/api/v1/x/{a}/metadata": {"get": {"operationId": "literal"}},
        }
    }
    templates = compile_templates(document)
    assert match(templates, "GET", "/api/v1/x/1/metadata") == "literal"
    assert match(templates, "GET", "/api/v1/x/1/2") == "params"
    assert match(templates, "POST", "/api/v1/x/1/2") is None


def test_the_query_string_is_ignored_and_internal_paths_never_match(
    templates: list[Template],
) -> None:
    assert match(templates, "GET", "/api/v1/audit/events?limit=5&cursor=x") is not None
    assert match(templates, "GET", "/internal/v1/events") is None
    assert match(templates, "POST", "/internal/v1/authz/check") is None


async def test_the_registry_holds_only_method_and_path_of_answered_requests(
    gateway: httpx.AsyncClient,
    admin_token: str,
    operation_registry: set[tuple[str, str]],
    operation_refusals: set[tuple[str, str]],
) -> None:
    secret = "registry-must-not-keep-this"
    probe = f"/api/v1/audit/events/{uuid.uuid4()}"
    refused = await gateway.get(probe, headers={"Authorization": f"Bearer {secret}"})
    assert refused.status_code == 401, refused.text
    # WR-05: a refusal is never coverage evidence.
    assert ("GET", probe) not in operation_registry
    assert ("GET", probe) in operation_refusals
    answered = await gateway.get(
        "/api/v1/audit/events",
        params={"limit": 1, "probe": secret},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert answered.status_code == 200, answered.text
    assert ("GET", "/api/v1/audit/events") in operation_registry
    for entry in operation_registry | operation_refusals:
        assert isinstance(entry, tuple) and len(entry) == 2, entry
        method, path = entry
        assert method.isupper() and path.startswith("/"), entry
        assert "?" not in path and secret not in path, entry


# --- report and gate --------------------------------------------------------------------------


def test_the_coverage_report_is_written(
    request: pytest.FixtureRequest,
    templates: list[Template],
    operation_registry: set[tuple[str, str]],
    operation_refusals: set[tuple[str, str]],
) -> None:
    every_id = {t.operation_id for t in templates}
    covered, unmatched = covered_operations(templates, operation_registry)
    refused, _ = covered_operations(templates, operation_refusals)
    exemptions = _exemptions()
    complete, missing = _all_journeys_collected(request.session)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "all_journeys_collected": complete,
        "journeys_not_collected": missing,
        "operations": len(every_id),
        "requests_recorded": len(operation_registry),
        "requests_unmatched": unmatched,
        "covered_count": len(covered & every_id),
        "uncovered_count": len(every_id - covered),
        "covered": sorted(covered & every_id),
        "uncovered": sorted(every_id - covered),
        "uncovered_not_exempt": sorted(every_id - covered - set(exemptions)),
        "requests_refused": len(operation_refusals),
        # Operations some e2e test called but never got a 2xx or 3xx from (WR-05).
        "refused_only": sorted((refused & every_id) - covered),
        "exempt": sorted(set(exemptions) & every_id),
        "stale_exemptions": sorted(set(exemptions) & covered),
        "unknown_exemptions": sorted(set(exemptions) - every_id),
    }
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def test_every_public_operation_is_covered_or_exempt(
    request: pytest.FixtureRequest,
    templates: list[Template],
    operation_registry: set[tuple[str, str]],
) -> None:
    complete, missing = _all_journeys_collected(request.session)
    if not complete:
        pytest.skip(f"coverage gate needs the whole suite; not collected: {', '.join(missing)}")
    every_id = {t.operation_id for t in templates}
    covered, _ = covered_operations(templates, operation_registry)
    exemptions = _exemptions()
    unknown = sorted(set(exemptions) - every_id)
    assert not unknown, f"exemptions for operations not in the merged document: {unknown}"
    gaps = sorted(every_id - covered - set(exemptions))
    assert not gaps, (
        "public operations neither exercised by an e2e test nor listed in "
        f"operation_exemptions.json with a reason: {gaps}"
    )
