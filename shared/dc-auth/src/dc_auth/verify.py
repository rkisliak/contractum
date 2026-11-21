"""Token verification: issuer registry + async JWKS cache -> Principal (T-01-02).

Only the token header is read before verification, and only to find the signing key (``alg`` and
``kid``); the payload is never read unverified. The issuer is chosen by full verification:
among the configured issuers that allow the ``alg`` and whose key set knows the ``kid``, the one
whose key verifies the signature and whose explicit ``algorithms``, ``audience``, ``issuer`` and
required claims all hold. Every failure is the same generic 401 (an IdP outage is 503); the token
is never echoed.
"""

import uuid
from typing import Any

import jwt
import structlog
from dc_core.problems import ProblemError

from dc_auth.issuers import ENTRA_PREFIX, IssuerConfig, IssuerRegistry
from dc_auth.jwks import JwksCache, JwksUnavailableError, KeyNotFoundError
from dc_auth.opaque import DCP_PREFIX, OpaqueTokenResolver
from dc_auth.principal import PRINCIPAL_TYPES, Principal, PrincipalType

_log = structlog.get_logger("dc_auth.verify")
_MAX_TOKEN_BYTES = 8192
_LEEWAY_S = 30


def unauthenticated() -> ProblemError:
    return ProblemError(
        401,
        "unauthenticated",
        "Unauthorized",
        "Authentication required or credentials invalid.",
        headers={"WWW-Authenticate": "Bearer"},
    )


class TokenVerifier:
    def __init__(
        self,
        registry: IssuerRegistry,
        jwks: JwksCache,
        opaque: OpaqueTokenResolver | None = None,
    ) -> None:
        self._registry = registry
        self._jwks = jwks
        self._opaque = opaque

    async def verify(self, token: str) -> Principal:
        try:
            return await self._verify(token)
        except ProblemError:
            raise
        except JwksUnavailableError as exc:
            # An IdP outage is not a bad credential: 503 so clients retry instead of dropping a
            # valid token. Still fail-closed: nothing is accepted without a verified signature.
            _log.warning("token_verification_unavailable", reason=type(exc).__name__)
            raise ProblemError(
                503,
                "service-unavailable",
                "Service Unavailable",
                "Authentication is temporarily unavailable.",
            ) from exc
        except (jwt.PyJWTError, KeyNotFoundError, ValueError, KeyError, TypeError) as exc:
            _log.info("token_rejected", reason=type(exc).__name__)
            raise unauthenticated() from exc

    async def _verify(self, token: str) -> Principal:
        if not token or len(token) > _MAX_TOKEN_BYTES:
            raise ValueError("token size")
        if token.startswith(DCP_PREFIX):
            # No JWT starts with "dcp_" (JWTs start "eyJ"): never parse an opaque token as one.
            if self._opaque is None:
                raise ValueError("opaque tokens not supported")
            return await self._opaque.resolve(token)
        header = jwt.get_unverified_header(token)
        alg = header.get("alg")
        if not isinstance(alg, str):
            raise ValueError("algorithm not allowed")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise ValueError("kid missing")
        candidates = self._registry.for_algorithm(alg)
        if not candidates:
            raise ValueError("algorithm not allowed")
        # The issuers whose key set already holds this kid own the token. With none (a rotation or
        # a cold start) every candidate may be asked; get_key rate limits the fetches per issuer.
        owners = [cfg for cfg in candidates if self._jwks.knows(cfg, kid)]
        unavailable: JwksUnavailableError | None = None
        failure: Exception = ValueError("no issuer verifies the token")
        for cfg in owners or candidates:
            try:
                key = await self._jwks.get_key(cfg, kid)
            except JwksUnavailableError as exc:
                unavailable = exc
                continue
            except KeyNotFoundError as exc:
                failure = exc
                continue
            try:
                claims: dict[str, Any] = jwt.decode(
                    token,
                    key,
                    algorithms=list(cfg.algorithms),
                    audience=list(cfg.audiences),
                    issuer=cfg.issuer,
                    options={"require": list(cfg.required_claims)},
                    leeway=_LEEWAY_S,
                )
            except jwt.PyJWTError as exc:
                failure = exc  # not this issuer's token (or not a valid one)
                continue
            # Issuers are unique, so the first verified token is the only possible owner: a
            # mapper failure is a rejection, never a reason to try another issuer.
            if cfg.kind == "entra":
                return _entra_principal(cfg, claims)
            return _principal_from_claims(cfg, claims)
        if unavailable is not None:
            # The token may belong to the issuer that is down: fail closed with a retryable 503.
            raise unavailable
        raise failure


def _issued_at(claims: dict[str, Any]) -> int:
    """The verified ``iat`` claim in whole seconds (required by both issuer kinds)."""
    iat = claims.get("iat")
    if isinstance(iat, bool) or not isinstance(iat, int | float):
        raise ValueError("iat")
    return int(iat)


def _entra_principal(cfg: IssuerConfig, claims: dict[str, Any]) -> Principal:
    """Strict Entra v2 access-token mapper (D-58, D-60, D-81).

    The only key is ``(tid, oid)``. ``ptype``, ``scope``, ``groups`` and ``roles`` are never read;
    ``scp`` and ``idtyp`` only classify the token. The verifier sets the scopes itself.
    """
    if claims.get("ver") != "2.0" or claims.get("tid") != cfg.tenant_id:
        raise ValueError("ver/tid")
    oid_raw = claims.get("oid")
    if not isinstance(oid_raw, str):
        raise ValueError("oid")
    oid = str(uuid.UUID(oid_raw))  # ValueError when not a GUID
    if oid != oid_raw:
        raise ValueError("oid form")  # only the lowercase canonical form is a key
    idtyp, scp = claims.get("idtyp"), claims.get("scp")
    ptype: PrincipalType
    if idtyp == "app" and "scp" not in claims:
        ptype = "service_principal"
    elif idtyp in (None, "user") and isinstance(scp, str) and scp.split():
        ptype = "user"  # ID tokens and daemon tokens have no scp
    else:
        raise ValueError("not an access token for this API")
    uti = claims.get("uti")
    name = claims.get("name")
    hint = claims.get("preferred_username")
    return Principal(
        id=f"{ENTRA_PREFIX}:{cfg.tenant_id}:{oid}",
        type=ptype,
        display=(name if isinstance(name, str) and name else oid)[:200],
        issuer=cfg.issuer,
        subject=oid,
        via="jwt",
        scopes=frozenset({"api"}),  # never derived from the token
        credential_id=uti if isinstance(uti, str) else None,
        username_hint=hint[:256] if isinstance(hint, str) and hint else None,
        issued_at=_issued_at(claims),
    )


def _principal_from_claims(cfg: IssuerConfig, claims: dict[str, Any]) -> Principal:
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise ValueError("sub")
    ptype = claims.get("ptype", cfg.default_principal_type)
    if not isinstance(ptype, str) or ptype not in PRINCIPAL_TYPES:
        raise ValueError("ptype")
    name = claims.get("name")
    display = name if isinstance(name, str) and name else sub
    scope = claims.get("scope", "")
    if not isinstance(scope, str):
        raise ValueError("scope")
    jti = claims.get("jti")
    return Principal(
        id=f"{cfg.namespace}:{sub}",
        type=ptype,
        display=display[:200],
        issuer=cfg.issuer,
        subject=sub,
        via="jwt",
        scopes=frozenset(scope.split()),
        credential_id=jti if isinstance(jti, str) else None,
        issued_at=_issued_at(claims),
    )


__all__ = ["PrincipalType", "TokenVerifier", "unauthenticated"]
