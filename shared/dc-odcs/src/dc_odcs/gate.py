"""Raw bytes -> strictly parsed, gated ODCS document (pure and synchronous).

``parse_contract`` is the only entry point the contracts service needs for ingest. It never
touches the network or a database; the service wraps it in ``asyncio.to_thread``.

Order of checks (D-19: only parse errors and the apiVersion/kind/id gate block):
media type -> empty body -> size cap -> strict UTF-8 -> parse -> mapping root -> JSON-native
walk (no exotic YAML types, no NUL, no lone surrogates, no non-finite floats) ->
apiVersion/kind/id gate.
JSON-Schema violations are *not* checked here; see ``dc_odcs.validate``.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

import yaml
import yaml.reader

from dc_odcs.errors import ContractRejected, malformed
from dc_odcs.loader import load_json, load_yaml

SUPPORTED_API_VERSIONS: frozenset[str] = frozenset({"v3.1.0", "v3.2.0"})
ALLOWED_MEDIA_TYPES: frozenset[str] = frozenset(
    {"application/yaml", "application/x-yaml", "text/yaml", "application/json"}
)

MAX_ODCS_ID_LENGTH = 255
"""The one bound on a contract's ODCS ``id``: the ingest gate and every API model use it."""
_BOM = "\ufeff"
_SURROGATE_MESSAGE = "Lone UTF-16 surrogate characters (for example \\ud800) are not allowed"


@dataclass(frozen=True)
class ParsedContract:
    document: dict[str, Any]
    api_version: str
    odcs_id: str
    kind: str
    media_type: str
    content_sha256: str
    size_bytes: int


def _pointer_escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _normalise_media_type(media_type: str) -> str:
    return media_type.split(";", 1)[0].strip().lower()


def _yaml_position(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return ""
    return f" (line {mark.line + 1}, column {mark.column + 1})"


def _parse_text(text: str, media_type: str) -> Any:
    if media_type == "application/json":
        try:
            return load_json(text)
        except json.JSONDecodeError as exc:
            raise malformed(
                f"Invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
            ) from exc
        except ValueError as exc:
            raise malformed(f"Invalid JSON: {exc}") from exc
        except RecursionError as exc:
            raise malformed("Invalid JSON: nesting is too deep") from exc
    try:
        return load_yaml(text)
    except yaml.YAMLError as exc:
        problem = getattr(exc, "problem", None)
        if problem is None and isinstance(exc, yaml.reader.ReaderError):
            problem = f"{exc.reason} (character offset {exc.position})"
        problem = problem or "syntax error"
        raise malformed(f"Invalid YAML: {problem}{_yaml_position(exc)}") from exc
    except ValueError as exc:
        raise malformed(f"Invalid YAML: {exc}") from exc
    except RecursionError as exc:
        raise malformed("Invalid YAML: nesting is too deep") from exc


def _has_lone_surrogate(text: str) -> bool:
    """True for a lone UTF-16 surrogate (``"\\ud800"``): it cannot be encoded as UTF-8."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _check_json_native(root: Any) -> None:
    """Reject anything jsonb cannot store faithfully. Iterative: no recursion limit to hit."""
    stack: list[tuple[Any, str]] = [(root, "")]
    while stack:
        node, pointer = stack.pop()
        if node is None or isinstance(node, bool | int):
            continue
        if isinstance(node, float):
            if not math.isfinite(node):
                raise malformed("Non-finite numbers are not allowed", pointer or None)
            continue
        if isinstance(node, str):
            if "\x00" in node:
                raise malformed("NUL (U+0000) characters are not allowed", pointer or None)
            if _has_lone_surrogate(node):
                raise malformed(_SURROGATE_MESSAGE, pointer or None)
            continue
        if isinstance(node, list):
            stack.extend((item, f"{pointer}/{index}") for index, item in enumerate(node))
            continue
        if isinstance(node, dict):
            for key, value in node.items():
                if not isinstance(key, str):
                    raise malformed("Mapping keys must be strings", pointer or None)
                key_pointer = f"{pointer}/{_pointer_escape(key)}"
                if "\x00" in key:
                    raise malformed("NUL (U+0000) characters are not allowed", key_pointer)
                if _has_lone_surrogate(key):
                    raise malformed(_SURROGATE_MESSAGE, key_pointer)
                stack.append((value, key_pointer))
            continue
        raise malformed(
            f"Unsupported value type {type(node).__name__!r}; only JSON-compatible values "
            "are allowed",
            pointer or None,
        )


def _gate(document: dict[str, Any], supported: frozenset[str]) -> tuple[str, str, str]:
    api_version = document.get("apiVersion")
    if not isinstance(api_version, str) or api_version not in supported:
        shown = "missing" if "apiVersion" not in document else repr(api_version)[:40]
        raise ContractRejected(
            status=422,
            slug="unsupported-odcs-version",
            title="Unsupported ODCS version",
            detail=(
                f"apiVersion is {shown}. Supported ODCS versions: {', '.join(sorted(supported))}"
            ),
            pointer="/apiVersion",
        )
    kind = document.get("kind")
    if kind != "DataContract":
        raise ContractRejected(
            status=422,
            slug="validation-error",
            title="Validation error",
            detail="kind must be the string 'DataContract'",
            pointer="/kind",
        )
    odcs_id = document.get("id")
    if not isinstance(odcs_id, str) or not odcs_id or len(odcs_id) > MAX_ODCS_ID_LENGTH:
        raise ContractRejected(
            status=422,
            slug="validation-error",
            title="Validation error",
            detail=f"id must be a non-empty string of at most {MAX_ODCS_ID_LENGTH} characters",
            pointer="/id",
        )
    return api_version, kind, odcs_id


def parse_contract(
    raw: bytes,
    media_type: str,
    *,
    supported: frozenset[str] = SUPPORTED_API_VERSIONS,
    max_bytes: int | None = None,
) -> ParsedContract:
    """Parse and gate ``raw``. Raises ``ContractRejected``; never raises for schema errors."""
    normalised = _normalise_media_type(media_type)
    if normalised not in ALLOWED_MEDIA_TYPES:
        raise ContractRejected(
            status=415,
            slug="unsupported-media-type",
            title="Unsupported media type",
            detail=(
                f"Content-Type {normalised or '(none)'!r} is not supported. Use one of: "
                f"{', '.join(sorted(ALLOWED_MEDIA_TYPES))}"
            ),
        )
    if not raw:
        raise malformed("The request body is empty")
    if max_bytes is not None and len(raw) > max_bytes:
        raise ContractRejected(
            status=413,
            slug="payload-too-large",
            title="Payload too large",
            detail=f"The document exceeds the {max_bytes}-byte limit",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise malformed(f"The document is not valid UTF-8 (byte offset {exc.start})") from exc
    if text.startswith(_BOM):
        text = text[1:]  # parsing only: stored bytes and content_sha256 keep the BOM

    parsed = _parse_text(text, normalised)
    if not isinstance(parsed, dict):
        raise malformed("The document root must be a mapping")
    _check_json_native(parsed)
    api_version, kind, odcs_id = _gate(parsed, supported)
    return ParsedContract(
        document=parsed,
        api_version=api_version,
        odcs_id=odcs_id,
        kind=kind,
        media_type=normalised,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
    )
