"""Tracer: Client.whoami through BearerAuth, the transport and _api to a mocked server."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import respx
from support.payloads import load_payload

from contractum.auth.tokens import StaticToken
from contractum.client import Client
from contractum.models import Identity

BASE = "https://contractum.example"


def test_whoami_sends_one_bearer_get_and_returns_a_typed_identity(canary_token: str) -> None:
    with respx.mock(base_url=BASE) as router:
        route = router.get("/api/v1/identity/me").respond(200, json=load_payload("me"))
        with Client(BASE, token=canary_token) as client:
            identity = client.whoami()
    assert route.call_count == 1
    request = route.calls.last.request
    assert request.method == "GET"
    assert request.headers["authorization"] == f"Bearer {canary_token}"
    assert isinstance(identity, Identity)
    assert identity.username == "dana"
    assert identity.idp == "local"
    assert identity.read_only is False
    assert identity.groups[0].type == "local_group"
    assert identity.assignments[0].folder_path == "acme/payments/ingest"


def test_repr_of_client_and_token_never_contains_the_token(
    canary_token: str, assert_no_secret: Callable[..., None]
) -> None:
    client = Client(BASE, token=canary_token)
    assert_no_secret(repr(client), canary_token)
    assert_no_secret(repr(StaticToken(canary_token, kind="env")), canary_token)
    client.close()


def test_product_header_names_the_package(canary_token: str) -> None:
    with respx.mock(base_url=BASE) as router:
        route = router.get("/api/v1/identity/me").respond(200, json=load_payload("me"))
        with Client(BASE, token=canary_token) as client:
            client.whoami()
    assert route.calls.last.request.headers["user-agent"].startswith("contractum/")


def test_an_injected_http_client_is_used_and_not_closed(canary_token: str) -> None:
    http = httpx.Client(base_url=BASE)
    with respx.mock(base_url=BASE) as router:
        router.get("/api/v1/identity/me").respond(200, json=load_payload("me"))
        client = Client(BASE, token=canary_token, http_client=http)
        client.whoami()
        client.close()
    assert not http.is_closed
    http.close()
