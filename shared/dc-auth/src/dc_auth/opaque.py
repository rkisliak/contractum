"""Opaque ``dcp_`` bearer tokens: personal access tokens and service-account keys (D-71).

Wire format ``dcp_<public_id>_<secret>``: ``public_id`` is 16 lowercase hex characters and the
secret is 43 URL-safe characters (``secrets.token_urlsafe(32)``), which may contain ``_``. This is a
leaf module: it imports dc_core and the principal and wire models, never verify or deps.
"""

import re
from typing import Protocol

import httpx
import structlog
from dc_core.problems import ProblemError
from pydantic import ValidationError

from dc_auth.authz_wire import PrincipalOut, passthrough_problem
from dc_auth.principal import Principal

_log = structlog.get_logger("dc_auth.opaque")
_PRINCIPAL_PATH = "/internal/v1/authz/principal"

DCP_PREFIX = "dcp_"
OPAQUE_ISSUER = "urn:dc:opaque"
PUBLIC_ID_RE = re.compile(r"^[0-9a-f]{16}$")
SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def parse_dcp(token: str) -> tuple[str, str] | None:
    """``(public_id, secret)`` of a well-formed ``dcp_`` token, else None.

    Splits on the first two underscores only, so a secret containing ``_`` still parses.
    """
    if not token.startswith(DCP_PREFIX):
        return None
    parts = token.split("_", 2)
    if len(parts) != 3:
        return None
    _, public_id, secret = parts
    if PUBLIC_ID_RE.fullmatch(public_id) is None or SECRET_RE.fullmatch(secret) is None:
        return None
    return public_id, secret


class OpaqueTokenResolver(Protocol):
    """Turns a ``dcp_`` token into a Principal; ValueError means rejected (401)."""

    async def resolve(self, token: str) -> Principal: ...


def _unavailable() -> ProblemError:
    return ProblemError(
        503,
        "service-unavailable",
        "Service Unavailable",
        "Authentication is temporarily unavailable.",
    )


class RemoteOpaqueResolver:
    """Resolves a ``dcp_`` token by forwarding it to identity on every request (D-71, D-72).

    No state is kept between calls, so a revoked or deactivated credential stops working on its
    next request. Identity trouble of any kind is a 503 (fail closed), never an allow, and the
    token is never logged or echoed.
    """

    def __init__(self, http: httpx.AsyncClient, *, base_url: str, timeout_s: float) -> None:
        self._http = http
        self._url = f"{base_url.rstrip('/')}{_PRINCIPAL_PATH}"
        self._timeout_s = timeout_s

    async def resolve(self, token: str) -> Principal:
        try:
            response = await self._http.post(
                self._url,
                content=b"{}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=self._timeout_s,
            )
        except httpx.HTTPError as exc:
            _log.warning("opaque_resolve_unreachable", error=type(exc).__name__)
            raise _unavailable() from None
        if response.status_code == 401:
            raise ValueError("opaque token rejected")
        problem = passthrough_problem(response.status_code, response.content)
        if problem is not None:
            raise problem
        if response.status_code != 200:
            _log.warning("opaque_resolve_bad_status", status=response.status_code)
            raise _unavailable()
        try:
            out = PrincipalOut.model_validate_json(response.content)
        except ValidationError:
            _log.warning("opaque_resolve_invalid_body")
            raise _unavailable() from None
        return Principal(
            id=out.id,
            type=out.type,
            display=out.display,
            issuer=out.issuer,
            subject=out.subject,
            via=out.via,
            scopes=frozenset({"api"}),
            credential_id=out.credential_id,
        )
