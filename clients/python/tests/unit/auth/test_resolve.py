"""resolve_token_provider: the order of token sources and the server binding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from contractum.auth import resolve_token_provider
from contractum.auth.store import SecretStore, StoredCredential
from contractum.auth.tokens import StaticToken
from contractum.config import Settings
from contractum.errors import AuthError

BASE = "https://contractum.example"
LOGIN_HINT = "run `contractum login --token -` or set CONTRACTUM_TOKEN"


def _settings(*, env_token: str | None = None) -> Settings:
    return Settings(
        profile="default",
        url=BASE,
        env_token=SecretStr(env_token) if env_token else None,
    )


@pytest.fixture
def store(tmp_path: Path) -> SecretStore:
    found = SecretStore(
        path=tmp_path / "data" / "credentials.json",
        env={"CONTRACTUM_KEYRING": "0"},
        warn_func=lambda _: None,
    )
    found.put(
        "default",
        StoredCredential(kind="pat", server=BASE, access_token=SecretStr("stored-pat")),
    )
    return found


def test_the_environment_token_wins_over_the_stored_one(store: SecretStore) -> None:
    provider = resolve_token_provider(_settings(env_token="env-token"), store=store)
    assert isinstance(provider, StaticToken)
    assert provider.kind == "env"
    assert provider.get_token() == "env-token"


@pytest.mark.parametrize("kind", ["pat", "service_account", "token", "entra_token"])
def test_a_stored_token_of_every_kind_is_a_static_token(store: SecretStore, kind: str) -> None:
    store.put(
        "default",
        StoredCredential(kind=kind, server=BASE, access_token=SecretStr("at")),  # type: ignore[arg-type]
    )
    provider = resolve_token_provider(_settings(), store=store)
    assert isinstance(provider, StaticToken)
    assert provider.kind == kind
    assert provider.get_token() == "at"
    assert provider.refresh() is None


def test_a_stored_entry_of_an_unknown_kind_cannot_be_used(store: SecretStore) -> None:
    legacy = {"kind": "entra_user", "server": BASE, "access_token": "at", "msal_cache": "{}"}
    store.credentials_path.write_text(json.dumps({"default": legacy}))
    with pytest.raises(AuthError, match="cannot be used") as raised:
        resolve_token_provider(_settings(), store=store)
    assert raised.value.hint == "run `contractum login --token -`"


def test_a_stored_entry_with_extra_old_fields_still_loads(store: SecretStore) -> None:
    entry = {"kind": "pat", "server": BASE, "access_token": "at", "expires_at": None}
    store.credentials_path.write_text(json.dumps({"default": entry}))
    assert resolve_token_provider(_settings(), store=store).kind == "pat"


def test_a_stored_credential_is_used_only_for_its_server(store: SecretStore) -> None:
    other = Settings(profile="default", url="https://other.example")
    with pytest.raises(AuthError, match=r"belongs to https://contractum\.example") as raised:
        resolve_token_provider(other, store=store)
    assert raised.value.hint == "run `contractum login --token -` for this server"
    assert "stored-pat" not in str(raised.value)


def test_a_trailing_slash_or_case_does_not_break_the_server_binding(store: SecretStore) -> None:
    same = Settings(profile="default", url="https://Contractum.example/")
    assert resolve_token_provider(same, store=store).kind == "pat"


def test_without_any_source_the_error_says_how_to_sign_in(tmp_path: Path) -> None:
    empty = SecretStore(
        path=tmp_path / "none" / "credentials.json", env={"CONTRACTUM_KEYRING": "0"}
    )
    with pytest.raises(AuthError, match="no token is configured") as raised:
        resolve_token_provider(_settings(), store=empty)
    assert raised.value.hint == LOGIN_HINT


def test_the_default_store_is_the_isolated_one() -> None:
    with pytest.raises(AuthError, match="no token is configured"):
        resolve_token_provider(_settings())
