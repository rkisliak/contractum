"""The only place httpx clients are built (CLAUDE.md: swap httpx for httpx2 in one file)."""

from typing import Any

import httpx


def make_client(*, timeout_s: float = 5.0, **kwargs: Any) -> httpx.AsyncClient:
    kwargs.setdefault("follow_redirects", False)
    return httpx.AsyncClient(timeout=timeout_s, **kwargs)
