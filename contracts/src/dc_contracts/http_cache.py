"""Draft ETags and ``If-Match`` evaluation (D-42, RFC 9110 sections 8.8.3 and 13.1.1).

Pure functions, no I/O. ``If-Match`` evaluation (``parse_etag_list``, ``if_match_ok``,
``precondition_required``, ``require_if_match``) is shared code in ``dc_core.conditional`` and is
re-exported here. ``If-None-Match`` (``if_none_match_hit``) is the read-side counterpart.
A draft's ETag is the strong tag ``"r<revision>"`` scoped to the draft
URL; converted renderings of the document carry a ``-json`` / ``-yaml`` suffix inside the quotes.
``If-Match`` uses the *strong* comparison: a weak tag (``W/"r1"``) never matches, ``*`` matches an
existing resource, and a malformed header matches nothing.

The problems raised here never echo the received header value.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from email.utils import format_datetime

# ``If-Match`` evaluation lives in dc_core so every service shares it; the names stay importable
# from here for the existing callers.
from dc_core.conditional import (
    if_match_ok,
    parse_etag_list,
    precondition_required,
    require_if_match,
)


def etag_for_draft(revision: int, target: str = "original") -> str:
    """The quoted strong ETag of a draft at ``revision``; ``target`` is original, json or yaml."""
    suffix = "" if target == "original" else f"-{target}"
    return f'"r{revision}{suffix}"'


def version_etag(content_sha256: str, target: str = "original") -> str:
    """The quoted strong ETag of a published version: its content hash, suffixed for conversions.

    Published bytes never change, so the hash identifies the original rendering; ``-json`` and
    ``-yaml`` mark converted renderings (D-47).
    """
    suffix = "" if target == "original" else f"-{target}"
    return f'"{content_sha256}{suffix}"'


def deprecation_headers(deprecated_at: datetime, sunset_date: date) -> dict[str, str]:
    """The ``Deprecation`` (RFC 9745) and ``Sunset`` (RFC 8594) headers of a deprecated version.

    ``Deprecation`` is the structured Date ``@<unix seconds>`` of the moment the version was first
    deprecated; ``Sunset`` is an HTTP-date at the end of the sunset day, UTC. Values come only from
    a stored timestamp and a validated date, never from user text, so nothing can inject a header.
    """
    end = datetime.combine(sunset_date, time(23, 59, 59), tzinfo=UTC)
    return {
        "Deprecation": f"@{int(deprecated_at.timestamp())}",
        "Sunset": format_datetime(end, usegmt=True),
    }


def if_none_match_hit(header: str | None, current: str) -> bool:
    """Whether an ``If-None-Match`` header matches ``current``, so a GET may answer 304.

    RFC 9110 section 13.1.2 uses the *weak* comparison: ``W/"a"`` matches ``"a"``, any element of
    a list may match, and ``*`` matches any current representation. An absent, blank or malformed
    header matches nothing, so the caller serves the full 200.
    """
    if header is None:
        return False
    parsed = parse_etag_list(header)
    if parsed is None:
        return False
    if parsed == "*":
        return True
    current_opaque = current.removeprefix("W/").strip('"')
    return any(opaque == current_opaque for _weak, opaque in parsed)


__all__ = [
    "deprecation_headers",
    "etag_for_draft",
    "if_match_ok",
    "if_none_match_hit",
    "parse_etag_list",
    "precondition_required",
    "require_if_match",
    "version_etag",
]
