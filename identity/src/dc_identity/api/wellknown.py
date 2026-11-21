"""Service-level well-known documents (not routed by the gateway)."""

from __future__ import annotations

from typing import Any

from dc_core.policy import PolicyRouter, public
from fastapi import Request, Response

router = PolicyRouter()


@router.get("/.well-known/jwks.json", dependencies=[public()], include_in_schema=False)
async def jwks(request: Request, response: Response) -> dict[str, Any]:
    """The current RS256 public key and, when configured, the previous one (D-03)."""
    response.headers["Cache-Control"] = "public, max-age=300"
    keys: dict[str, Any] = request.app.state.signing_keys.jwks()
    return keys
