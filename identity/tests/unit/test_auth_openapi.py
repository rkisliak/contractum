"""The identity OpenAPI document declares the browser cookie contract (UI-01, D-UI-08-12/13).

The UI's generated client is built from the merged document, so the opt-in header, the cookie
parameter, the ``Set-Cookie`` response headers and the extra statuses must be visible there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from dc_identity.main import create_identity_app
from dc_identity.settings import IdentitySettings

pytestmark = pytest.mark.unit

BASE = "/api/v1/identity/auth"
PROBLEM = {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}}


@pytest.fixture(scope="module")
def document(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    tmp_path: Path = tmp_path_factory.mktemp("openapi")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_file = tmp_path / "jwt_signing_key.pem"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    # The database is never connected: only the route table and schema are read.
    settings = IdentitySettings(
        database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/never",  # type: ignore[arg-type]
        jwt_signing_key_file=key_file,
        bootstrap_secret_file=tmp_path / "absent",
    )
    doc: dict[str, Any] = create_identity_app(settings).openapi()
    return doc


def _op(document: dict[str, Any], name: str) -> dict[str, Any]:
    op: dict[str, Any] = document["paths"][f"{BASE}/{name}"]["post"]
    return op


def _param(op: dict[str, Any], name: str, where: str) -> dict[str, Any] | None:
    for param in op.get("parameters", []):
        if param["name"] == name and param["in"] == where:
            found: dict[str, Any] = param
            return found
    return None


def _enum_values(schema: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for branch in schema.get("anyOf", [schema]):
        if "const" in branch:
            values.add(branch["const"])
        values.update(branch.get("enum", []))
    return values


@pytest.mark.parametrize("name", ["login", "refresh", "logout"])
def test_every_cookie_aware_operation_declares_the_delivery_header(
    document: dict[str, Any], name: str
) -> None:
    param = _param(_op(document, name), "X-Token-Delivery", "header")
    assert param is not None
    assert param["required"] is False
    assert _enum_values(param["schema"]) == {"cookie"}


@pytest.mark.parametrize("name", ["refresh", "logout"])
def test_refresh_and_logout_declare_the_cookie_parameter(
    document: dict[str, Any], name: str
) -> None:
    param = _param(_op(document, name), "__Secure-dc_refresh", "cookie")
    assert param is not None
    assert param["required"] is False
    assert _param(_op(document, "login"), "__Secure-dc_refresh", "cookie") is None


@pytest.mark.parametrize(
    ("name", "success"), [("login", "200"), ("refresh", "200"), ("logout", "204")]
)
def test_the_success_response_declares_the_set_cookie_header(
    document: dict[str, Any], name: str, success: str
) -> None:
    headers = _op(document, name)["responses"][success]["headers"]
    assert "Set-Cookie" in headers
    assert headers["Set-Cookie"]["schema"] == {"type": "string"}
    assert "__Secure-dc_refresh" in headers["Set-Cookie"]["description"]


@pytest.mark.parametrize("name", ["login", "refresh", "logout"])
def test_the_csrf_and_validation_statuses_are_declared_as_problems(
    document: dict[str, Any], name: str
) -> None:
    responses = _op(document, name)["responses"]
    for status in ("403", "422"):
        assert status in responses, f"{name} lacks {status}"
        assert responses[status]["content"] == PROBLEM
    assert "csrf-check-failed" in responses["403"]["description"]


@pytest.mark.parametrize("name", ["login", "refresh"])
def test_login_and_refresh_declare_401(document: dict[str, Any], name: str) -> None:
    assert _op(document, name)["responses"]["401"]["content"] == PROBLEM


def test_a_failed_refresh_declares_the_clearing_set_cookie_header(
    document: dict[str, Any],
) -> None:
    assert "Set-Cookie" in _op(document, "refresh")["responses"]["401"]["headers"]
