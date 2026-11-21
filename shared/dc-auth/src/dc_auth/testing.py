"""Test support: an in-process issuer (RSA key + JWKS) that mints tokens, and its fixture.

Loaded through the root ``conftest.py``. Covers expired, wrong-audience, wrong-issuer,
forged and malformed tokens deterministically, and mints the non-admin principals used for
authenticated-but-unauthorized tests (D-06: no user creation in Phase 1).
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from dc_auth.authz_wire import PrincipalResolveOut, RefState
from dc_auth.issuers import IssuerConfig
from dc_auth.principal import Principal

_ISSUER = "https://fake-issuer.test"

ENTRA_TENANT_ID = "4f1c2d3e-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
ENTRA_AUDIENCE = "6b7c8d9e-0f1a-4b2c-9d3e-4f5a6b7c8d9e"
_ENTRA_ISSUER_FMT = "https://login.microsoftonline.com/{tid}/v2.0"


def _kid_for(public_numbers_jwk: Mapping[str, Any]) -> str:
    """RFC 7638 thumbprint of an RSA public key."""
    canon = json.dumps(
        {k: public_numbers_jwk[k] for k in ("e", "kty", "n")}, separators=(",", ":"), sort_keys=True
    ).encode()
    return base64.urlsafe_b64encode(hashlib.sha256(canon).digest()).rstrip(b"=").decode()


class FakeIssuer:
    issuer = _ISSUER
    namespace = "fake"
    jwks_uri = f"{_ISSUER}/.well-known/jwks.json"

    def __init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._private_pem = self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key()))
        self.kid = _kid_for(jwk)
        jwk.update(kid=self.kid, use="sig", alg="RS256")
        self._jwk = jwk

    def jwks(self) -> dict[str, Any]:
        return {"keys": [dict(self._jwk)]}

    def issuer_config(self, *, static: bool = True) -> IssuerConfig:
        return IssuerConfig(
            issuer=self.issuer,
            jwks_uri=self.jwks_uri,
            audiences=("contractum",),
            namespace=self.namespace,
            static_jwks=self.jwks() if static else None,
        )

    @property
    def entra_issuer(self) -> str:
        """The v2 issuer URL of the default test tenant."""
        return _ENTRA_ISSUER_FMT.format(tid=ENTRA_TENANT_ID)

    def entra_issuer_config(
        self, *, tenant_id: str = ENTRA_TENANT_ID, namespace: str = "entra"
    ) -> IssuerConfig:
        """An Entra-kind issuer entry for ``tenant_id`` signed by this issuer's RSA key."""
        issuer = _ENTRA_ISSUER_FMT.format(tid=tenant_id)
        return IssuerConfig(
            issuer=issuer,
            jwks_uri=f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
            audiences=(ENTRA_AUDIENCE,),
            namespace=namespace,
            kind="entra",
            tenant_id=tenant_id,
            static_jwks=self.jwks(),
        )

    def mint_entra(
        self,
        oid: str,
        *,
        token_type: str = "user",  # noqa: S107 - a token kind, not a password
        tid: str | None = None,
        aud: str | None = None,
        iss: str | None = None,
        ver: str = "2.0",
        scp: str | None = "access_as_user",
        idtyp: str | None = None,
        name: str = "Entra User",
        preferred_username: str = "user@example.test",
        exp_delta_s: int = 900,
        nbf_delta_s: int = 0,
        drop_claims: Iterable[str] = (),
        extra_claims: Mapping[str, Any] | None = None,
    ) -> str:
        """Mint an Entra v2 access token: ``uti`` instead of ``jti``, no ptype or scope claims."""
        tenant = tid or ENTRA_TENANT_ID
        now = int(time.time())
        app_only = token_type == "app"  # noqa: S105
        claims: dict[str, Any] = {
            "iss": iss or _ENTRA_ISSUER_FMT.format(tid=tenant),
            "aud": aud or ENTRA_AUDIENCE,
            "iat": now,
            "nbf": now + nbf_delta_s,
            "exp": now + exp_delta_s,
            "sub": oid if app_only else "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "oid": oid,
            "tid": tenant,
            "ver": ver,
            "uti": uuid.uuid4().hex,
            "name": name,
        }
        if app_only:
            claims["roles"] = ["Contracts.Access"]
            claims["idtyp"] = idtyp or "app"
        else:
            claims["preferred_username"] = preferred_username
            if scp is not None:
                claims["scp"] = scp
            if idtyp is not None:
                claims["idtyp"] = idtyp
        claims.update(extra_claims or {})
        for key in drop_claims:
            claims.pop(key, None)
        return jwt.encode(claims, self._private_pem, algorithm="RS256", headers={"kid": self.kid})

    def _claims(
        self,
        sub: str,
        *,
        ptype: str,
        scope: str,
        name: str,
        aud: str,
        exp_delta_s: int,
        nbf_delta_s: int,
        iss: str,
        drop_claims: Iterable[str],
        extra_claims: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": iss,
            "sub": sub,
            "aud": aud,
            "iat": now,
            "nbf": now + nbf_delta_s,
            "exp": now + exp_delta_s,
            "jti": str(uuid.uuid4()),
            "ptype": ptype,
            "scope": scope,
            "name": name,
        }
        claims.update(extra_claims or {})
        for key in drop_claims:
            claims.pop(key, None)
        return claims

    def mint(
        self,
        sub: str,
        *,
        ptype: str = "user",
        scope: str = "api",
        name: str = "Test User",
        aud: str = "contractum",
        exp_delta_s: int = 900,
        nbf_delta_s: int = 0,
        alg: str = "RS256",
        drop_claims: Iterable[str] = (),
        headers: Mapping[str, Any] | None = None,
        iss: str | None = None,
        extra_claims: Mapping[str, Any] | None = None,
    ) -> str:
        claims = self._claims(
            sub,
            ptype=ptype,
            scope=scope,
            name=name,
            aud=aud,
            exp_delta_s=exp_delta_s,
            nbf_delta_s=nbf_delta_s,
            iss=iss or self.issuer,
            drop_claims=drop_claims,
            extra_claims=extra_claims,
        )
        hdrs: dict[str, Any] = {"kid": self.kid}
        hdrs.update(headers or {})
        hdrs = {k: v for k, v in hdrs.items() if v is not None}  # None removes a header
        if alg == "RS256":
            return jwt.encode(claims, self._private_pem, algorithm="RS256", headers=hdrs)
        if alg == "none":
            return jwt.encode(claims, "", algorithm="none", headers=hdrs)
        if alg == "HS256":
            return jwt.encode(claims, "x" * 32, algorithm="HS256", headers=hdrs)
        raise ValueError(f"unsupported alg for the fake issuer: {alg}")

    def hs256_forgery(self, sub: str) -> str:
        """Algorithm-confusion attempt: HS256 header, valid kid, attacker-chosen secret."""
        return self.mint(sub, alg="HS256")


class FakePrincipalDirectory:
    """In-process stand-in for ``RemoteAuthorizer.resolve_principals`` (configurable directory).

    ``users`` and ``groups`` are the names that exist (matched lower-cased and trimmed, like
    identity), ``unverifiable_users`` the names identity could not check (an Entra user it has never
    seen). Anything else is ``not_found``. Set ``outage`` to answer every name ``unverifiable``,
    as the real client does when identity is down.
    """

    def __init__(
        self,
        *,
        users: Iterable[str] = (),
        groups: Iterable[str] = (),
        unverifiable_users: Iterable[str] = (),
        outage: bool = False,
    ) -> None:
        self.users = {self._key(n) for n in users}
        self.groups = {self._key(n) for n in groups}
        self.unverifiable_users = {self._key(n) for n in unverifiable_users}
        self.outage = outage

    @staticmethod
    def _key(name: str) -> str:
        return name.strip().lower()

    def _user_state(self, name: str) -> RefState:
        key = self._key(name)
        if self.outage or (key not in self.users and key in self.unverifiable_users):
            return "unverifiable"
        return "found" if key in self.users else "not_found"

    def _group_state(self, name: str) -> RefState:
        if self.outage:
            return "unverifiable"
        return "found" if self._key(name) in self.groups else "not_found"

    async def resolve_principals(
        self, principal: Principal, users: Sequence[str], groups: Sequence[str]
    ) -> PrincipalResolveOut:
        return PrincipalResolveOut(
            users={name: self._user_state(name) for name in users},
            groups={name: self._group_state(name) for name in groups},
        )


_ISSUER_SINGLETON: FakeIssuer | None = None


@pytest.fixture(scope="session")
def fake_issuer() -> FakeIssuer:
    global _ISSUER_SINGLETON  # noqa: PLW0603 - one RSA key per session
    if _ISSUER_SINGLETON is None:
        _ISSUER_SINGLETON = FakeIssuer()
    return _ISSUER_SINGLETON
