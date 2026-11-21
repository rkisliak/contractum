"""Accept-driven document representation (D-11, CONT-06).

The default stays byte-faithful: no Accept preference, ``*/*``, or a type the document is already
stored as returns the submitted bytes untouched. Only an explicit preference for the *other*
format converts, and a conversion is re-rendered from the stored raw bytes with the strict ODCS
loaders, in original key order. The jsonb copy is never used: jsonb drops key order. A converted
rendering is not byte-faithful by definition.

Both functions are pure and synchronous; the route runs :func:`render` in ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
from typing import Literal

import yaml
from dc_core.problems import ProblemError
from dc_odcs import load_json, load_yaml

Target = Literal["original", "json", "yaml"]

JSON_TYPE = "application/json"
YAML_TYPE = "application/yaml"
YAML_FAMILY: frozenset[str] = frozenset({"application/yaml", "application/x-yaml", "text/yaml"})
_BOM = "\ufeff"


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _parse_accept(accept: str) -> list[tuple[str, float]]:
    """Media ranges with their quality; a malformed range or quality is skipped, never guessed."""
    ranges: list[tuple[str, float]] = []
    for part in accept.split(","):
        head, *params = (piece.strip() for piece in part.split(";"))
        media = head.lower()
        if media.count("/") != 1 or not all(media.split("/")):
            continue
        quality = 1.0
        valid = True
        for param in params:
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    valid = False
                if not 0.0 <= quality <= 1.0:
                    valid = False
        if valid:
            ranges.append((media, quality))
    return ranges


def _quality(ranges: list[tuple[str, float]], media_type: str) -> float:
    """Quality of the most specific range matching ``media_type``; 0 when none matches."""
    major = media_type.split("/", 1)[0]
    best_rank = -1
    best = 0.0
    for media, quality in ranges:
        if media == media_type:
            rank = 2
        elif media == f"{major}/*":
            rank = 1
        elif media == "*/*":
            rank = 0
        else:
            continue
        if rank > best_rank:
            best_rank, best = rank, quality
    return best


def negotiate(accept: str | None, stored_media_type: str) -> Target:
    """Pick the representation: ``original`` (stored bytes), ``json`` or ``yaml`` (conversion).

    An absent or blank Accept means the original. Otherwise the stored representation (any YAML
    alias for a YAML document) and the other format compete on quality, the most specific range
    deciding each one's quality, and a tie goes to the original. Nothing acceptable is a 406.
    """
    if accept is None or not accept.strip():
        return "original"
    stored = _media_type(stored_media_type)
    is_yaml = stored in YAML_FAMILY
    ranges = _parse_accept(accept)
    original_quality = max(
        _quality(ranges, alias) for alias in (YAML_FAMILY if is_yaml else frozenset({JSON_TYPE}))
    )
    other_types = frozenset({JSON_TYPE}) if is_yaml else YAML_FAMILY
    other_quality = max(_quality(ranges, media) for media in other_types)
    if original_quality <= 0.0 and other_quality <= 0.0:
        raise ProblemError(
            406,
            "not-acceptable",
            "Not Acceptable",
            f"Supported representations: {JSON_TYPE}, {YAML_TYPE} (default: as submitted).",
        )
    if other_quality > original_quality:
        return "json" if is_yaml else "yaml"
    return "original"


def render(raw: bytes, stored_media_type: str, target: str) -> tuple[bytes, str]:
    """Re-render the stored raw bytes as ``target`` (``json`` or ``yaml``) and its media type.

    The stored bytes were accepted by the strict loaders at ingest, so parsing them again cannot
    fail for a stored document.
    """
    text = raw.decode("utf-8")
    if text.startswith(_BOM):
        text = text[1:]
    stored = _media_type(stored_media_type)
    document = load_json(text) if stored == JSON_TYPE else load_yaml(text)
    if target == "json":
        body = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        return body.encode("utf-8"), JSON_TYPE
    if target == "yaml":
        body = yaml.safe_dump(
            document, sort_keys=False, allow_unicode=True, default_flow_style=False
        )
        return body.encode("utf-8"), YAML_TYPE
    raise ValueError(f"unknown conversion target {target!r}")


__all__ = ["JSON_TYPE", "YAML_FAMILY", "YAML_TYPE", "Target", "negotiate", "render"]
