"""The only place httpx clients are built (CLAUDE.md: swap httpx for httpx2 in one file)."""

from typing import Any

import httpx

# uvicorn closes a connection after 5 s idle, the same as httpx's default. With equal limits a
# request can go out on a connection the server is closing, which fails with ReadError and
# reaches the user as a 503. The client must always give up on an idle connection first.
KEEPALIVE_EXPIRY_S = 2.0

# httpx's default pool sizes, with only the idle limit changed.
_LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=KEEPALIVE_EXPIRY_S,
)


def make_client(*, timeout_s: float = 5.0, **kwargs: Any) -> httpx.AsyncClient:
    kwargs.setdefault("follow_redirects", False)
    kwargs.setdefault("limits", _LIMITS)
    return httpx.AsyncClient(timeout=timeout_s, **kwargs)
