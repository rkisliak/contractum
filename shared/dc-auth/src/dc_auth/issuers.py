"""Issuer registry: which token issuers are trusted and how their tokens are checked."""

import re
from typing import Any, Literal, Self

from pydantic import BaseModel, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ASYMMETRIC = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "EdDSA"}
)

IssuerKind = Literal["oidc", "entra"]

OIDC_REQUIRED_CLAIMS: tuple[str, ...] = ("exp", "iss", "aud", "sub", "iat", "jti")
# Entra v2 access tokens carry ``uti`` (their per-token id), never ``jti`` (D-58).
ENTRA_REQUIRED_CLAIMS: tuple[str, ...] = (
    "exp",
    "iss",
    "aud",
    "iat",
    "nbf",
    "sub",
    "oid",
    "tid",
    "ver",
)
# Every Entra principal id starts with this prefix, whatever namespace the issuer entry uses, so
# one parser reads every tenant's ids (D-58).
ENTRA_PREFIX = "entra"
# "sa" is minted only by identity's service-account credential resolver; "entra" only by the
# Entra mapper. No other issuer may produce ids in these forms (T-04-06).
RESERVED_NAMESPACES: frozenset[str] = frozenset({"sa"})
OIDC_RESERVED_NAMESPACES: frozenset[str] = frozenset({"entra", "sa"})

_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class IssuerConfig(BaseModel):
    issuer: str
    jwks_uri: str
    audiences: tuple[str, ...]
    algorithms: tuple[str, ...] = ("RS256",)
    namespace: str
    default_principal_type: str = "user"
    kind: IssuerKind = "oidc"
    tenant_id: str | None = None  # Entra only: the pinned tenant (lowercase GUID)
    required_claims: tuple[str, ...] = ()  # empty -> the kind's default, filled below
    static_jwks: dict[str, Any] | None = None

    @field_validator("algorithms")
    @classmethod
    def _asymmetric_only(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # HS* and "none" are never acceptable: they are the classic algorithm-confusion route.
        bad = [a for a in value if a not in _ASYMMETRIC]
        if bad or not value:
            raise ValueError(f"algorithms must be a non-empty subset of {sorted(_ASYMMETRIC)}")
        return value

    @field_validator("audiences")
    @classmethod
    def _audiences_present(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("at least one audience is required")
        return value

    @model_validator(mode="after")
    def _kind_rules(self) -> Self:
        # Fixed messages: configuration values are never echoed.
        ns = self.namespace.lower()
        if ns in RESERVED_NAMESPACES:
            raise ValueError("namespace is reserved")
        if self.kind == "oidc":
            if ns in OIDC_RESERVED_NAMESPACES:
                raise ValueError("namespace is reserved for the entra kind")
            if self.tenant_id is not None:
                raise ValueError("tenant_id is only valid for kind entra")
            if not self.required_claims:
                self.required_claims = OIDC_REQUIRED_CLAIMS
        else:
            if self.tenant_id is None or not _GUID.fullmatch(self.tenant_id):
                raise ValueError("kind entra requires a lowercase GUID tenant_id")
            if not self.required_claims:
                self.required_claims = ENTRA_REQUIRED_CLAIMS
            elif not {"tid", "oid", "ver"} <= set(self.required_claims):
                raise ValueError("kind entra requires the tid, oid and ver claims")
        return self


class IssuerRegistry:
    def __init__(self, configs: list[IssuerConfig] | tuple[IssuerConfig, ...]) -> None:
        self._by_issuer: dict[str, IssuerConfig] = {}
        namespaces: set[str] = set()
        tenants: set[str] = set()
        for cfg in configs:
            if cfg.issuer in self._by_issuer or cfg.namespace in namespaces:
                raise ValueError(f"duplicate issuer or namespace: {cfg.issuer} / {cfg.namespace}")
            if cfg.kind == "entra" and cfg.tenant_id is not None:
                if cfg.tenant_id in tenants:
                    raise ValueError("duplicate entra tenant")
                tenants.add(cfg.tenant_id)
            self._by_issuer[cfg.issuer] = cfg
            namespaces.add(cfg.namespace)
        self._tenants = tuple(sorted(tenants))

    def get(self, issuer: str) -> IssuerConfig | None:
        return self._by_issuer.get(issuer)

    def for_algorithm(self, alg: str) -> tuple[IssuerConfig, ...]:
        """The configured issuers that allow ``alg``, in registration order."""
        return tuple(cfg for cfg in self._by_issuer.values() if alg in cfg.algorithms)

    @property
    def entra_tenant_ids(self) -> tuple[str, ...]:
        """Sorted tenant ids of the kind "entra" entries."""
        return self._tenants


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DC_AUTH_", extra="ignore")

    issuers: list[IssuerConfig] = []
    identity_internal_url: str = "http://identity:8000"
    authz_timeout_s: float = 2.0
