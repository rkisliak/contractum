"""A capped request-body reader (T-01-43).

Routes that take a raw document authenticate first (the policy dependency runs before the handler)
and only then read the bytes here, so an unauthenticated client never makes a service buffer
anything. The cap is enforced twice: early on ``Content-Length`` and again on the stream, which is
authoritative.
"""

from __future__ import annotations

from fastapi import Request

from dc_core.problems import ProblemError


def _too_large(max_bytes: int, detail: str | None) -> ProblemError:
    return ProblemError(
        413,
        "payload-too-large",
        "Payload Too Large",
        detail if detail is not None else f"The document exceeds the {max_bytes}-byte limit.",
    )


async def read_body_capped(request: Request, max_bytes: int, *, detail: str | None = None) -> bytes:
    """Read the body, refusing early on Content-Length and aborting once the cap is passed."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            length = -1  # not a number: fall through to the streaming cap, which is authoritative
        if length > max_bytes:
            raise _too_large(max_bytes, detail)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise _too_large(max_bytes, detail)
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = ["read_body_capped"]
