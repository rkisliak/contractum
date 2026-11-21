"""Token providers and the bearer authentication used by the HTTP client.

A provider hands out the access token for the next request and, when the server answers 401,
may return a replacement. No class here prints, logs or stores a token value.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Protocol, runtime_checkable

import httpx

from contractum.errors import ConfigError

MAX_TOKEN_CHARS = 16_384


@runtime_checkable
class TokenProvider(Protocol):
    """A source of bearer tokens."""

    @property
    def kind(self) -> str:
        """A short label such as ``env`` or ``stored``; never part of the secret."""
        ...

    def get_token(self) -> str:
        """Return the access token to send now."""
        ...

    def refresh(self) -> str | None:
        """Return a new access token after a 401, or None when none can be obtained."""
        ...


class StaticToken:
    """A fixed token (a PAT, a service-account key or a pre-fetched JWT) that is never refreshed."""

    __slots__ = ("_token", "kind")

    def __init__(self, token: str, kind: str = "token") -> None:
        cleaned = token.strip()
        if not cleaned:
            raise ConfigError("the token is empty")
        if len(cleaned) > MAX_TOKEN_CHARS or not all(33 <= ord(ch) <= 126 for ch in cleaned):
            raise ConfigError("the token contains whitespace or characters that cannot be sent")
        self._token = cleaned
        self.kind = kind

    def get_token(self) -> str:
        return self._token

    def refresh(self) -> str | None:
        return None

    def __repr__(self) -> str:
        return f"StaticToken(kind={self.kind!r})"


class BearerAuth(httpx.Auth):
    """Send ``Authorization: Bearer <token>`` and retry once with a refreshed token on a 401."""

    def __init__(self, provider: TokenProvider) -> None:
        self._provider = provider

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._provider.get_token()}"
        response = yield request
        if response.status_code != 401:
            return
        replacement = self._provider.refresh()
        if replacement is None:
            return
        request.headers["Authorization"] = f"Bearer {replacement}"
        yield request

    def __repr__(self) -> str:
        return f"BearerAuth(kind={self._provider.kind!r})"
