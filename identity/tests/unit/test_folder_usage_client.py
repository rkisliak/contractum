"""HttpFolderUsage: the caller bearer goes through, and any doubt is a 503 (quick 261119-0v1)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import respx
from dc_core.problems import ProblemError
from dc_identity.folder_usage import (
    CONTRACTS_PURGE_PATH,
    CONTRACTS_USAGE_PATH,
    VALIDATION_PURGE_PATH,
    VALIDATION_USAGE_PATH,
    HttpFolderUsage,
    RemoteFolderUsage,
    RemotePurge,
)

pytestmark = pytest.mark.unit

FOLDER = uuid.UUID("018f0000-0000-7000-8000-000000000001")
TOKEN = "caller-bearer-token"  # noqa: S105 - a test value
CONTRACTS = "http://contracts.test"
VALIDATION = "http://validation.test"

CONTRACTS_USAGE = {
    "folder_id": str(FOLDER),
    "contracts": 1,
    "templates": 2,
    "subscriber_cards": 3,
    "deleted_contracts": 4,
    "archived_subscriber_cards": 5,
}
VALIDATION_USAGE = {"folder_id": str(FOLDER), "rule_sets": 6, "unused_rule_sets": 7}
CONTRACTS_PURGED = {
    "folder_id": str(FOLDER),
    "deleted_contracts": 4,
    "contract_versions": 9,
    "archived_subscriber_cards": 5,
    "card_subscriptions": 2,
    "origin_cleared": 1,
}
VALIDATION_PURGED = {"folder_id": str(FOLDER), "rule_sets": 7, "rule_set_versions": 11}


@pytest.fixture
async def port() -> AsyncIterator[tuple[HttpFolderUsage, respx.MockRouter]]:
    with respx.mock(assert_all_called=False) as router:
        contracts = httpx.AsyncClient(base_url=CONTRACTS)
        validation = httpx.AsyncClient(base_url=VALIDATION)
        yield HttpFolderUsage(contracts, validation, timeout_s=1.0), router
        await contracts.aclose()
        await validation.aclose()


def _usage_routes(router: respx.MockRouter) -> tuple[respx.Route, respx.Route]:
    c = router.get(CONTRACTS + CONTRACTS_USAGE_PATH.format(folder_id=FOLDER)).respond(
        json=CONTRACTS_USAGE
    )
    v = router.get(VALIDATION + VALIDATION_USAGE_PATH.format(folder_id=FOLDER)).respond(
        json=VALIDATION_USAGE
    )
    return c, v


async def test_counts_merge_both_answers_and_forward_the_callers_bearer(
    port: tuple[HttpFolderUsage, respx.MockRouter],
) -> None:
    usage, router = port
    c, v = _usage_routes(router)
    got = await usage.counts(FOLDER, TOKEN)
    assert got == RemoteFolderUsage(
        contracts=1,
        templates=2,
        subscriber_cards=3,
        rule_sets=6,
        deleted_contracts=4,
        archived_subscriber_cards=5,
        unused_rule_sets=7,
    )
    for route in (c, v):
        assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"


async def test_purge_posts_to_both_services_with_the_callers_bearer(
    port: tuple[HttpFolderUsage, respx.MockRouter],
) -> None:
    usage, router = port
    c = router.post(CONTRACTS + CONTRACTS_PURGE_PATH.format(folder_id=FOLDER)).respond(
        json=CONTRACTS_PURGED
    )
    v = router.post(VALIDATION + VALIDATION_PURGE_PATH.format(folder_id=FOLDER)).respond(
        json=VALIDATION_PURGED
    )
    got = await usage.purge(FOLDER, TOKEN)
    assert got == RemotePurge(
        deleted_contracts=4,
        contract_versions=9,
        archived_subscriber_cards=5,
        card_subscriptions=2,
        origin_cleared=1,
        rule_sets=7,
        rule_set_versions=11,
    )
    for route in (c, v):
        assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"


def _failure(kind: str) -> Callable[[respx.Route], None]:
    def apply(route: respx.Route) -> None:
        if kind == "timeout":
            route.side_effect = httpx.ReadTimeout("slow")
        elif kind == "connect":
            route.side_effect = httpx.ConnectError("down")
        elif kind in ("500", "403", "404"):
            route.respond(int(kind))
        elif kind == "not-json":
            route.respond(200, content=b"<html>")
        elif kind == "list":
            route.respond(200, json=[1, 2])
        elif kind == "missing-key":
            route.respond(200, json={"folder_id": str(FOLDER)})
        elif kind == "negative":
            route.respond(200, json={**route_body(route), "contracts": -1, "rule_sets": -1})
        elif kind == "float":
            route.respond(200, json={**route_body(route), "contracts": 1.5, "rule_sets": 1.5})
        elif kind == "bool":
            route.respond(200, json={**route_body(route), "contracts": True, "rule_sets": True})
        elif kind == "other-folder":
            route.respond(200, json={**route_body(route), "folder_id": str(uuid.uuid4())})
        else:  # pragma: no cover
            raise AssertionError(kind)

    return apply


def route_body(route: respx.Route) -> dict[str, Any]:
    return CONTRACTS_USAGE if "contracts" in str(route.pattern) else VALIDATION_USAGE


KINDS = [
    "timeout",
    "connect",
    "500",
    "403",
    "404",
    "not-json",
    "list",
    "missing-key",
    "negative",
    "float",
    "bool",
    "other-folder",
]


@pytest.mark.parametrize("service", ["contracts", "validation"])
@pytest.mark.parametrize("kind", KINDS)
async def test_any_doubt_on_either_service_is_503_service_unavailable(
    port: tuple[HttpFolderUsage, respx.MockRouter], service: str, kind: str
) -> None:
    usage, router = port
    c, v = _usage_routes(router)
    _failure(kind)(c if service == "contracts" else v)
    with pytest.raises(ProblemError) as caught:
        await usage.counts(FOLDER, TOKEN)
    assert caught.value.status == 503
    assert caught.value.slug == "service-unavailable"
    assert TOKEN not in str(caught.value.detail)


@pytest.mark.parametrize("service", ["contracts", "validation"])
async def test_a_purge_that_fails_on_either_service_is_503(
    port: tuple[HttpFolderUsage, respx.MockRouter], service: str
) -> None:
    usage, router = port
    c = router.post(CONTRACTS + CONTRACTS_PURGE_PATH.format(folder_id=FOLDER)).respond(
        json=CONTRACTS_PURGED
    )
    v = router.post(VALIDATION + VALIDATION_PURGE_PATH.format(folder_id=FOLDER)).respond(
        json=VALIDATION_PURGED
    )
    (c if service == "contracts" else v).respond(409)
    with pytest.raises(ProblemError) as caught:
        await usage.purge(FOLDER, TOKEN)
    assert caught.value.status == 503


@pytest.mark.parametrize("service", ["contracts", "validation"])
async def test_a_401_from_either_service_is_the_401_unauthenticated_problem(
    port: tuple[HttpFolderUsage, respx.MockRouter], service: str
) -> None:
    usage, router = port
    c, v = _usage_routes(router)
    (c if service == "contracts" else v).respond(401)
    with pytest.raises(ProblemError) as caught:
        await usage.counts(FOLDER, TOKEN)
    assert caught.value.status == 401
