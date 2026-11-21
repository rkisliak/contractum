"""Issuer configuration rules for the Entra kind and reserved namespaces (AUTH-03, T-04-06)."""

from __future__ import annotations

import json

import pytest
from dc_auth.issuers import (
    ENTRA_REQUIRED_CLAIMS,
    OIDC_REQUIRED_CLAIMS,
    AuthSettings,
    IssuerConfig,
    IssuerRegistry,
)
from dc_auth.testing import ENTRA_TENANT_ID, FakeIssuer
from pydantic import ValidationError

pytestmark = pytest.mark.unit

TENANT_B = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _cfg(**overrides: object) -> IssuerConfig:
    base: dict[str, object] = {
        "issuer": "https://idp.test",
        "jwks_uri": "https://idp.test/jwks",
        "audiences": ("aud",),
        "namespace": "idp",
    }
    base.update(overrides)
    return IssuerConfig(**base)  # type: ignore[arg-type]


def _entra(**overrides: object) -> IssuerConfig:
    base: dict[str, object] = {
        "issuer": f"https://login.microsoftonline.com/{ENTRA_TENANT_ID}/v2.0",
        "kind": "entra",
        "tenant_id": ENTRA_TENANT_ID,
        "namespace": "entra",
    }
    base.update(overrides)
    return _cfg(**base)


def test_oidc_default_required_claims_are_unchanged_and_include_jti() -> None:
    assert _cfg().required_claims == OIDC_REQUIRED_CLAIMS
    assert "jti" in OIDC_REQUIRED_CLAIMS


def test_entra_default_required_claims_hold_no_jti() -> None:
    cfg = _entra()
    assert cfg.required_claims == ENTRA_REQUIRED_CLAIMS
    assert "jti" not in ENTRA_REQUIRED_CLAIMS
    assert {"tid", "oid", "ver", "nbf"} <= set(ENTRA_REQUIRED_CLAIMS)


@pytest.mark.parametrize(
    "overrides",
    [
        {"tenant_id": None},
        {"tenant_id": "ABC"},
        {"tenant_id": ENTRA_TENANT_ID.upper()},
        {"required_claims": ("exp", "iss", "aud", "tid", "ver")},  # no oid
        {"required_claims": ("exp", "iss", "aud", "oid", "ver")},  # no tid
        {"required_claims": ("exp", "iss", "aud", "oid", "tid")},  # no ver
        {"namespace": "sa"},
        {"namespace": "SA"},
    ],
)
def test_invalid_entra_entries_are_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _entra(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"namespace": "sa"},
        {"namespace": "entra"},
        {"namespace": "ENTRA"},
        {"namespace": "Entra"},
        {"tenant_id": ENTRA_TENANT_ID},
    ],
)
def test_oidc_entries_cannot_mint_reserved_forms(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _cfg(**overrides)


def test_validation_errors_do_not_echo_the_configured_value() -> None:
    with pytest.raises(ValidationError) as info:
        _entra(tenant_id="secret-looking-value")
    # Our validator's own message is fixed; (pydantic's input dump is not ours to control).
    assert all("secret-looking-value" not in str(e["msg"]) for e in info.value.errors())


def test_an_entra_entry_keeps_explicit_required_claims_holding_tid_oid_and_ver() -> None:
    claims = ("exp", "iss", "aud", "iat", "nbf", "tid", "oid", "ver", "uti")
    assert _entra(required_claims=claims).required_claims == claims


def test_an_entra_namespace_is_configurable() -> None:
    assert _entra(namespace="entra-b").namespace == "entra-b"


def test_registry_refuses_two_entries_for_the_same_tenant() -> None:
    first = _entra()
    second = _entra(issuer="https://login.microsoftonline.com/other/v2.0", namespace="entra-dup")
    with pytest.raises(ValueError, match="duplicate entra tenant"):
        IssuerRegistry([first, second])


def test_registry_exposes_sorted_entra_tenants(fake_issuer: FakeIssuer) -> None:
    registry = IssuerRegistry(
        [
            fake_issuer.issuer_config(),
            fake_issuer.entra_issuer_config(tenant_id=ENTRA_TENANT_ID),
            fake_issuer.entra_issuer_config(tenant_id=TENANT_B, namespace="entra-b"),
        ]
    )
    assert registry.entra_tenant_ids == tuple(sorted((ENTRA_TENANT_ID, TENANT_B)))
    assert IssuerRegistry([fake_issuer.issuer_config()]).entra_tenant_ids == ()


def test_registry_still_refuses_duplicate_issuers_and_namespaces() -> None:
    with pytest.raises(ValueError, match="duplicate issuer or namespace"):
        IssuerRegistry([_cfg(), _cfg(namespace="other")])
    with pytest.raises(ValueError, match="duplicate issuer or namespace"):
        IssuerRegistry([_cfg(), _cfg(issuer="https://idp2.test")])


def test_auth_settings_parse_an_entra_entry_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = {
        "issuer": f"https://login.microsoftonline.com/{TENANT_B}/v2.0",
        "jwks_uri": f"https://login.microsoftonline.com/{TENANT_B}/discovery/v2.0/keys",
        "audiences": ["6b7c8d9e-0f1a-4b2c-9d3e-4f5a6b7c8d9e"],
        "namespace": "entra-b",
        "kind": "entra",
        "tenant_id": TENANT_B,
    }
    monkeypatch.setenv("DC_AUTH_ISSUERS", json.dumps([entry]))
    settings = AuthSettings()
    (cfg,) = settings.issuers
    assert cfg.kind == "entra"
    assert cfg.tenant_id == TENANT_B
    assert cfg.namespace == "entra-b"
    assert cfg.required_claims == ENTRA_REQUIRED_CLAIMS
    assert IssuerRegistry(settings.issuers).entra_tenant_ids == (TENANT_B,)


def test_an_explicit_asymmetric_algorithm_list_is_kept() -> None:
    assert _cfg(algorithms=("RS256", "ES256")).algorithms == ("RS256", "ES256")


@pytest.mark.parametrize(
    "algorithms",
    [(), ("HS256",), ("none",), ("RS256", "HS512")],
    ids=["empty", "hs256", "none", "mixed-with-hmac"],
)
def test_symmetric_none_or_empty_algorithm_lists_are_refused(
    algorithms: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError, match="algorithms must be a non-empty subset"):
        _cfg(algorithms=algorithms)


def test_an_entry_without_an_audience_is_refused() -> None:
    with pytest.raises(ValidationError, match="at least one audience is required"):
        _cfg(audiences=())


def test_registry_get_returns_the_entry_for_a_configured_issuer_only() -> None:
    cfg = _cfg()
    registry = IssuerRegistry([cfg])
    assert registry.get("https://idp.test") is cfg
    assert registry.get("https://unknown.test") is None
