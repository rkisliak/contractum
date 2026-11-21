"""RFC 9457 problem details (PLAT-07, D-18).

Every client-visible error is ``application/problem+json`` with a ``urn:dc:problem:<slug>``
type. The 422 body carries ``errors[{pointer, detail}]`` and never echoes submitted input;
the 500 body carries a generic detail only.
"""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

import structlog
from asgi_correlation_id import correlation_id
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_MEDIA_TYPE = "application/problem+json"

_log = structlog.get_logger("dc_core.problems")

_STATUS_SLUGS: dict[int, str] = {
    400: "bad-request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not-found",
    405: "method-not-allowed",
    409: "conflict",
    413: "payload-too-large",
    415: "unsupported-media-type",
    422: "validation-error",
    429: "too-many-requests",
    503: "service-unavailable",
}


def problem_type(slug: str) -> str:
    return f"urn:dc:problem:{slug}"


def _title(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def problem_response(
    status: int,
    slug: str,
    title: str,
    detail: str | None = None,
    *,
    request_id: str | None = None,
    headers: Mapping[str, str] | None = None,
    instance: str | None = None,
    **extensions: Any,
) -> JSONResponse:
    """Render a problem body; usable outside exception handlers (ASGI middleware)."""
    body: dict[str, Any] = {"type": problem_type(slug), "title": title, "status": status}
    if detail is not None:
        body["detail"] = detail
    if instance is not None:
        body["instance"] = instance
    rid = request_id or correlation_id.get()
    if rid:
        body["request_id"] = rid
    body.update(extensions)
    return JSONResponse(
        body,
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=dict(headers) if headers else None,
    )


class ProblemError(Exception):
    """Raise anywhere in a request to produce a problem+json response."""

    def __init__(
        self,
        status: int,
        slug: str,
        title: str,
        detail: str | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        **extensions: Any,
    ) -> None:
        super().__init__(f"{status} {slug}: {title}")
        self.status = status
        self.slug = slug
        self.title = title
        self.detail = detail
        self.headers = dict(headers) if headers else None
        self.extensions = extensions


def _pointer(loc: tuple[Any, ...]) -> str:
    parts = (str(p).replace("~", "~0").replace("/", "~1") for p in loc)
    return "/" + "/".join(parts)


async def _problem_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ProblemError)  # noqa: S101 - handler registration guarantees it
    return problem_response(
        exc.status,
        exc.slug,
        exc.title,
        exc.detail,
        headers=exc.headers,
        instance=request.url.path,
        **exc.extensions,
    )


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    status = exc.status_code
    slug = _STATUS_SLUGS.get(status) or ("internal-error" if status >= 500 else "bad-request")
    detail = exc.detail if isinstance(exc.detail, str) else None
    headers = getattr(exc, "headers", None)
    return problem_response(
        status,
        slug,
        _title(status),
        detail,
        headers=headers,
        instance=request.url.path,
    )


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    # Only the location and the validator message: never ``input`` or ``ctx``, which carry
    # the submitted value.
    errors = [
        {"pointer": _pointer(tuple(err.get("loc", ()))), "detail": str(err.get("msg", "invalid"))}
        for err in exc.errors()
    ]
    return problem_response(
        422,
        "validation-error",
        "Unprocessable Content",
        "The request failed validation.",
        instance=request.url.path,
        errors=errors,
    )


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    # Runs in ServerErrorMiddleware, outside user middleware: request_id comes from the
    # correlation contextvar, which CorrelationIdMiddleware leaves set.
    _log.error("unhandled_exception", exc_info=exc, path=request.url.path)
    return problem_response(
        500,
        "internal-error",
        "Internal Server Error",
        "Internal server error",
        instance=request.url.path,
    )


def install_problem_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ProblemError, _problem_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(Exception, _unhandled_handler)
