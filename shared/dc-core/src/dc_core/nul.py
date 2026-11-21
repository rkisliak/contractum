"""Guard for request strings PostgreSQL cannot store: NUL (U+0000) and lone UTF-16 surrogates.

PostgreSQL cannot store a NUL character: psycopg raises ``DataError`` when one is bound to a text
value and the server refuses ``\\u0000`` in ``jsonb``. A lone surrogate code point (U+D800 to
U+DFFF, for example JSON ``"\\ud800"``) is accepted by Pydantic but cannot be encoded as UTF-8, so
psycopg raises ``UnicodeEncodeError``. Either way the caller would see a 500. Every service route
is a ``NulGuardRoute`` (the default route class of ``PolicyRouter``), which refuses either with a
422 ``validation-error`` before authentication, dependencies or the database run:

* in any query parameter name or value;
* in any string or object key of a JSON body that FastAPI parses for a body parameter.

The names keep the ``nul`` prefix from the first version; every helper covers both cases.

The path is already covered by ``EncodedPathGuard`` (``%00`` is a 400 ``invalid-path``). Routes
that read a raw body themselves (``read_body_capped``) parse it with their own loader and call
:func:`find_unstorable` (contract ingest does it in ``dc_odcs``). DTOs built outside a route can use
:data:`NoNulStr`. The problem never echoes the submitted value.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Coroutine
from email.message import Message
from json import JSONDecodeError, detect_encoding
from typing import Annotated, Any

from fastapi import Request, Response, params
from fastapi.routing import APIRoute
from pydantic import AfterValidator

from dc_core.problems import ProblemError

NUL_DETAIL = "NUL (U+0000) characters are not allowed"
SURROGATE_DETAIL = "Lone UTF-16 surrogate characters (U+D800 to U+DFFF) are not allowed"

_SURROGATE = re.compile("[\ud800-\udfff]")

# A UTF-8 JSON text can only carry a NUL as the escape ``\u0000`` (a raw control character inside a
# string is invalid JSON) and a surrogate as a ``\ud800``-``\udfff`` escape (hex digits in either
# case) or as the raw bytes ED A0..BF: ``json.loads`` decodes bytes with ``surrogatepass``, so
# strictly invalid UTF-8 still reaches the handler. A body matching neither needs no walk. A body
# in UTF-16 or UTF-32 (which ``json.loads`` also accepts) is always walked.
_JSON_SUSPECT = re.compile(rb"\\u(?:0000|[dD][89a-fA-F])|\xed[\xa0-\xbf]")
_UTF8 = frozenset({"utf-8", "utf-8-sig"})


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def unstorable_detail(text: str) -> str | None:
    """:data:`NUL_DETAIL` or :data:`SURROGATE_DETAIL` when ``text`` holds one, else None."""
    if "\x00" in text:
        return NUL_DETAIL
    if _SURROGATE.search(text) is not None:
        return SURROGATE_DETAIL
    return None


def find_unstorable(value: Any) -> tuple[str, str] | None:
    """``(pointer, detail)`` of the first string or mapping key holding a NUL or lone surrogate.

    Iterative, so a deep document cannot hit the recursion limit. An offending key is reported at
    its parent's pointer, so the pointer itself never carries the character.
    """
    stack: list[tuple[Any, str]] = [(value, "")]
    while stack:
        node, pointer = stack.pop()
        if isinstance(node, str):
            detail = unstorable_detail(node)
            if detail is not None:
                return pointer, detail
        elif isinstance(node, dict):
            for key, item in node.items():
                if isinstance(key, str):
                    detail = unstorable_detail(key)
                    if detail is not None:
                        return pointer, detail
                stack.append((item, f"{pointer}/{_escape(str(key))}"))
        elif isinstance(node, list | tuple):
            stack.extend((item, f"{pointer}/{index}") for index, item in enumerate(node))
    return None


def find_nul(value: Any) -> str | None:
    """Pointer of the first NUL or lone surrogate (see :func:`find_unstorable`), or None."""
    found = find_unstorable(value)
    return None if found is None else found[0]


def nul_problem(
    pointer: str, *, slug: str = "validation-error", detail: str = NUL_DETAIL
) -> ProblemError:
    """The 422 for an unstorable character at ``pointer`` (``detail`` says which kind).

    ``slug`` lets a document route keep its own slug.
    """
    title = "Malformed document" if slug == "malformed-document" else "Unprocessable Content"
    top = detail if slug == "malformed-document" else "The request failed validation."
    return ProblemError(
        422, slug, title, top, errors=[{"pointer": pointer or "/", "detail": detail}]
    )


def reject_nul(value: str) -> str:
    """Return ``value``, or raise ``ValueError`` (a Pydantic 422) for a NUL or lone surrogate."""
    detail = unstorable_detail(value)
    if detail is not None:
        raise ValueError(detail)
    return value


NoNulStr = Annotated[str, AfterValidator(reject_nul)]
"""A ``str`` refusing NUL and lone surrogates, for DTOs validated outside a route's body."""


def _query_problem(request: Request) -> tuple[str, str] | None:
    for name, item in request.query_params.multi_items():
        detail = unstorable_detail(name)
        if detail is not None:
            return "/query", detail
        detail = unstorable_detail(item)
        if detail is not None:
            return f"/query/{_escape(name)}", detail
    return None


def _needs_walk(raw: bytes) -> bool:
    return _JSON_SUSPECT.search(raw) is not None or detect_encoding(raw) not in _UTF8


def _is_json(content_type: str | None) -> bool:
    if not content_type:
        return False
    message = Message()
    message["content-type"] = content_type
    subtype = message.get_content_subtype()
    return message.get_content_maintype() == "application" and (
        subtype == "json" or subtype.endswith("+json")
    )


class NulGuardRoute(APIRoute):
    """APIRoute refusing NUL and lone surrogates in query parameters and a parsed JSON body."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        body_field = self.body_field
        parses_json = body_field is not None and not isinstance(body_field.field_info, params.Form)
        # FastAPI also parses a body with no Content-Type as JSON when strict_content_type is off.
        lenient = self.strict_content_type is False

        async def guarded(request: Request) -> Response:
            found = _query_problem(request)
            if found is not None:
                raise nul_problem(found[0], detail=found[1])
            # Only for routes whose JSON body FastAPI reads anyway (before authentication); a
            # raw-body route reads its own capped body after authentication and is not touched.
            content_type = request.headers.get("content-type")
            if parses_json and (_is_json(content_type) or (lenient and not content_type)):
                raw = await request.body()
                if _needs_walk(raw):
                    try:
                        data = await request.json()  # cached: FastAPI's own read reuses it
                    except (JSONDecodeError, UnicodeDecodeError):
                        data = None  # FastAPI renders its own 422 for undecodable JSON
                    found = find_unstorable(data)
                    if found is not None:
                        raise nul_problem(f"/body{found[0]}", detail=found[1])
            return await handler(request)

        return guarded


__all__ = [
    "NUL_DETAIL",
    "SURROGATE_DETAIL",
    "NoNulStr",
    "NulGuardRoute",
    "find_nul",
    "find_unstorable",
    "nul_problem",
    "reject_nul",
    "unstorable_detail",
]
