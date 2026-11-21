"""``If-Match`` evaluation shared by every service (D-42, RFC 9110 sections 8.8.3 and 13.1.1).

Pure functions, no I/O. ``If-Match`` uses the *strong* comparison: a weak tag (``W/"r1"``) never
matches, ``*`` matches an existing resource, and a malformed header matches nothing. The problems
raised here never echo the received header value.
"""

from __future__ import annotations

import re
from typing import Literal

from dc_core.problems import ProblemError

# entity-tag = [ weak ] opaque-tag ; opaque-tag = DQUOTE *etagc DQUOTE
# etagc = %x21 / %x23-7E / obs-text (no DQUOTE, no space, no control characters)
_ENTITY_TAG = r'(?:W/)?"[\x21\x23-\x7e\x80-\xff]*"'
_LIST = re.compile(rf"^\s*{_ENTITY_TAG}(?:\s*,\s*{_ENTITY_TAG})*\s*$")
_ONE = re.compile(r'(W/)?"([^"]*)"')


def parse_etag_list(header: str) -> list[tuple[bool, str]] | Literal["*"] | None:
    """Parse an entity-tag list into ``(is_weak, opaque)`` pairs.

    A lone ``*`` returns the literal ``"*"``. A blank header or any malformed element (unquoted,
    unterminated, empty element) returns None.
    """
    if header.strip() == "*":
        return "*"
    if not _LIST.match(header):
        return None
    return [(bool(weak), opaque) for weak, opaque in _ONE.findall(header)]


def if_match_ok(header: str | None, current: str) -> bool:
    """Strong comparison of an ``If-Match`` header against the current (quoted) ETag."""
    if header is None:
        return False
    parsed = parse_etag_list(header)
    if parsed is None:
        return False
    if parsed == "*":
        return True
    current_opaque = current.removeprefix("W/").strip('"')
    return any(not weak and opaque == current_opaque for weak, opaque in parsed)


def precondition_required() -> ProblemError:
    return ProblemError(
        428,
        "precondition-required",
        "Precondition Required",
        "This request must carry an If-Match header with the current ETag.",
    )


def require_if_match(header: str | None, current: str) -> None:
    """428 when ``If-Match`` is absent or blank, 412 when it does not match ``current``."""
    if header is None or not header.strip():
        raise precondition_required()
    if not if_match_ok(header, current):
        raise ProblemError(
            412,
            "precondition-failed",
            "Precondition Failed",
            "The resource has changed since you last read it; reload it and retry.",
        )


__all__ = ["if_match_ok", "parse_etag_list", "precondition_required", "require_if_match"]
