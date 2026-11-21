"""Token sign-in: verify then store, and logout that only forgets the stored token."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import respx
from pydantic import SecretStr
from support.payloads import load_payload

from contractum.auth.local import login_token, logout, pat_label
from contractum.auth.store import SecretStore, StoredCredential
from contractum.config import Settings
from contractum.errors import AuthError, ConfigError

BASE = "https://contractum.example"
ME = "/api/v1/identity/me"
SETTINGS = Settings(profile="default", url=BASE)
PROBLEM = {"content-type": "application/problem+json"}


def _me(**changes: Any) -> dict[str, Any]:
    return {**load_payload("me"), "via": "pat", "read_only": True, **changes}


@pytest.fixture
def store(tmp_path: Path) -> SecretStore:
    return SecretStore(
        path=tmp_path / "data" / "credentials.json",
        env={"CONTRACTUM_KEYRING": "0"},
        warn_func=lambda _: None,
    )


def _put(store: SecretStore, kind: Any) -> None:
    store.put(
        "default",
        StoredCredential(kind=kind, server=BASE, access_token=SecretStr("tok-1")),
    )


# --- login_token ----------------------------------------------------------------------------


def test_pat_label_is_the_public_id_only() -> None:
    assert pat_label("dcp_0123456789abcdef_sec_ret-x") == "dcp_0123456789abcdef"
    assert pat_label("dcp_nosecret") is None
    assert pat_label("dcp__x") is None
    assert pat_label("eyJhbGciOi.payload.sig") is None


def test_login_token_refuses_unusable_tokens(store: SecretStore) -> None:
    for token in ("", "a b", "x" * (8 * 1024 + 1)):
        with pytest.raises(ConfigError):
            login_token(SETTINGS, token, store=store, profile="default")
    assert store.get("default") is None


@pytest.mark.parametrize(
    ("via", "idp", "kind"),
    [
        ("pat", "local", "pat"),
        ("key", "local", "service_account"),
        ("jwt", "local", "token"),
        ("jwt", "entra", "entra_token"),
    ],
)
def test_login_token_verifies_then_stores_the_kind_the_server_reports(
    store: SecretStore, via: str, idp: str, kind: str
) -> None:
    token = "dcp_0123456789abcdef_secret"
    with respx.mock(base_url=BASE) as router:
        route = router.get(ME).respond(200, json=_me(via=via, idp=idp))
        identity = login_token(SETTINGS, token, store=store, profile="default")
    assert route.calls.last.request.headers["authorization"] == f"Bearer {token}"
    assert identity.display_name == "Dana Engineer"
    stored = store.get("default")
    assert stored is not None
    assert stored.kind == kind
    assert stored.access_token.get_secret_value() == token
    label = "dcp_0123456789abcdef" if kind in ("pat", "service_account") else "Dana Engineer"
    assert stored.principal_label == label


def test_a_rejected_token_is_not_stored(store: SecretStore) -> None:
    with respx.mock(base_url=BASE) as router:
        router.get(ME).respond(401, json={"status": 401}, headers=PROBLEM)
        with pytest.raises(AuthError):
            login_token(SETTINGS, "tok-1", store=store, profile="default")
    assert store.get("default") is None


def test_the_stored_entry_holds_no_session_fields(store: SecretStore) -> None:
    with respx.mock(base_url=BASE) as router:
        router.get(ME).respond(200, json=_me())
        login_token(SETTINGS, "tok-1", store=store, profile="default")
    entry = json.loads(store.credentials_path.read_text())["default"]
    assert set(entry) == {"kind", "server", "access_token", "principal_label"}


# --- logout ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "hint"),
    [
        ("pat", "revoke it in the web interface"),
        ("service_account", "an administrator revokes it"),
        ("token", "stays valid until it expires or is revoked"),
        ("entra_token", "the token itself stays valid until it expires"),
    ],
)
def test_logout_forgets_the_token_without_a_request_and_says_it_stays_valid(
    store: SecretStore, kind: str, hint: str
) -> None:
    _put(store, kind)
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        message = logout(SETTINGS, store=store, profile="default")
        assert router.calls.call_count == 0
    assert "removed the stored credential for profile 'default'" in message
    assert hint in message
    assert store.get("default") is None
    assert "tok-1" not in message


def test_logout_removes_an_entry_of_a_kind_this_version_does_not_know(
    store: SecretStore,
) -> None:
    path = store.credentials_path
    path.parent.mkdir(parents=True)
    legacy = {"kind": "local_session", "server": BASE, "access_token": "a", "refresh_token": "r"}
    path.write_text(json.dumps({"default": legacy}))
    path.chmod(0o600)
    with pytest.raises(AuthError, match="cannot be used") as raised:
        store.get("default")
    assert raised.value.hint == "run `contractum login --token -`"
    message = logout(SETTINGS, store=store, profile="default")
    assert "removed the stored credential" in message
    assert "stays valid until it expires or is revoked" in message
    assert json.loads(path.read_text()) == {}


def test_logout_without_a_credential_and_with_the_environment_token(
    store: SecretStore,
) -> None:
    settings = Settings(profile="default", url=BASE, env_token=SecretStr("env-secret"))
    message = logout(settings, store=store, profile="default")
    assert "not signed in" in message
    assert "unset it to sign out there" in message
    assert "env-secret" not in message
