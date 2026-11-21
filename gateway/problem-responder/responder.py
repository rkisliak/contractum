"""Problem responder: RFC 9457 bodies for the errors the gateway itself generates (D-18).

Traefik's ``errors`` middleware (statuses 413, 429, 500-599) and the priority-1 catch-all router
(unrouted paths, rewritten to ``/404``) both send requests here. This is a raw ASGI callable on
the standard library only: no web framework and no import of the platform's shared libraries, so
the gateway stays independent of service code and a replacement gateway can drop it in.

Routing: ``/healthz`` answers 200; ``/<NNN>`` with NNN an integer from 400 to 599 renders status
NNN; anything else renders 404. Bodies are static per status. The path, the query and every
header except a well-formed ``X-Request-ID`` are never reflected (T-01-62).
"""

import json
import re
from typing import Any

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_STATUS_PATH = re.compile(r"^/([4-5][0-9]{2})$")

# status -> (slug, title, detail)
_KNOWN: dict[int, tuple[str, str, str]] = {
    404: ("not-found", "Not found", "The requested resource does not exist."),
    413: (
        "payload-too-large",
        "Request body too large",
        "The request body exceeds the maximum size accepted by the gateway.",
    ),
    429: (
        "too-many-requests",
        "Too many requests",
        "Too many requests were sent in a short time. Retry later.",
    ),
    500: ("internal-error", "Internal server error", "The server failed to process the request."),
    502: ("bad-gateway", "Bad gateway", "The gateway received an invalid response from upstream."),
    503: (
        "service-unavailable",
        "Service unavailable",
        "The service is temporarily unable to handle the request.",
    ),
    504: (
        "gateway-timeout",
        "Gateway timeout",
        "The upstream service did not respond in time.",
    ),
}
_GENERIC = ("gateway-error", "Gateway error", "The gateway could not complete the request.")

_HEADERS = [
    (b"content-type", b"application/problem+json"),
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
]


def _request_id(scope: dict[str, Any]) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"x-request-id":
            try:
                text = value.decode("ascii")
            except UnicodeDecodeError:
                return None
            return text if _REQUEST_ID.fullmatch(text) else None
    return None


def _status_for(path: str) -> int:
    match = _STATUS_PATH.match(path)
    return int(match.group(1)) if match else 404


async def _send(send: Any, status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> None:
    headers = [*headers, (b"content-length", str(len(body)).encode("ascii"))]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _lifespan(receive: Any, send: Any) -> None:
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] == "lifespan":
        await _lifespan(receive, send)
        return
    if scope["type"] != "http":
        return
    path = scope.get("path", "")
    if path == "/healthz":
        body = json.dumps({"status": "ok"}).encode("utf-8")
        await _send(send, 200, [(b"content-type", b"application/json")], body)
        return
    status = _status_for(path)
    slug, title, detail = _KNOWN.get(status, _GENERIC)
    problem: dict[str, Any] = {
        "type": f"urn:dc:problem:{slug}",
        "title": title,
        "status": status,
        "detail": detail,
    }
    request_id = _request_id(scope)
    if request_id is not None:
        problem["request_id"] = request_id
    await _send(send, status, _HEADERS, json.dumps(problem).encode("utf-8"))
