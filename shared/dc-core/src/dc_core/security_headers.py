"""Response hardening headers every service sets itself (quick 261120-pn7, 261120-s0p).

``X-Content-Type-Options: nosniff`` stops a browser from guessing a type for a JSON or problem
answer (ZAP rule 10021). ``Cache-Control: no-store`` keeps a JSON or problem answer out of every
browser and proxy cache (the audit's cache-control follow-up). Both are properties of the service,
not of the gateway: a replacement gateway must not be what makes them appear.

A header a handler already set is left alone, so a route that documents its own caching keeps it
(the JWKS, the version route, ``settings/public`` and the ETag contract fetches). The service's
own ``openapi.json`` is exempt through ``cacheable_paths``: it is a public document and stays
cacheable.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

NOSNIFF_HEADER = "X-Content-Type-Options"
NOSNIFF_VALUE = "nosniff"
CACHE_CONTROL_HEADER = "Cache-Control"
NO_STORE_VALUE = "no-store"


class SecurityHeadersMiddleware:
    """Pure ASGI middleware adding ``nosniff`` and ``Cache-Control: no-store`` to HTTP responses."""

    def __init__(self, app: ASGIApp, cacheable_paths: frozenset[str] = frozenset()) -> None:
        self.app = app
        self.cacheable_paths = cacheable_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cacheable = scope["path"] in self.cacheable_paths

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if NOSNIFF_HEADER not in headers:
                    headers[NOSNIFF_HEADER] = NOSNIFF_VALUE
                if not cacheable and CACHE_CONTROL_HEADER not in headers:
                    headers[CACHE_CONTROL_HEADER] = NO_STORE_VALUE
            await send(message)

        await self.app(scope, receive, send_with_headers)
