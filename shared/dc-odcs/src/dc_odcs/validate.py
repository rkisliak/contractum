"""Non-blocking structural validation against the vendored official ODCS JSON Schemas.

Schemas live in ``dc_odcs/schemas`` and are pinned by upstream commit and SHA-256 in
``MANIFEST.json`` (verified by a unit test). They are never fetched at runtime. The schema is
chosen by the document's own ``apiVersion`` (research Pitfall 5). Per D-19 a schema violation is
reported, never raised.
"""

from __future__ import annotations

import functools
import hashlib
import json
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any

import jsonschema_rs

MAX_REPORTED_ERRORS = 50
# jsonschema-rs messages embed the whole failing instance, so one error can be as large as the
# document. Bounding each message keeps the report (and the 201 body) small (WR-03).
MAX_MESSAGE_CHARS = 300
# Unknown-property errors name the offending keys (keys only, never values; quick 261119-wk8).
# Both bounds keep the message and the report small whatever the document holds.
MAX_NAMED_PROPERTIES = 5
MAX_PROPERTY_NAME_CHARS = 64
UNKNOWN_PROPERTY_KEYWORDS = frozenset({"additionalProperties", "unevaluatedProperties"})


@dataclass(frozen=True)
class StructuralError:
    pointer: str  # RFC 6901 JSON pointer into the document; "" is the document root
    message: str
    # The JSON Schema keyword that failed (last segment of the error's schema path: vendored
    # schema data, never document data). Validation reports show a fixed text per keyword.
    keyword: str = "schema"
    # For an unknown-property error: the first MAX_NAMED_PROPERTIES offending keys, cleaned and
    # bounded by ``clean_property_name`` (keys only, never values), and how many there were.
    unexpected: tuple[str, ...] = ()
    unexpected_count: int = 0


@dataclass(frozen=True)
class StructuralReport:
    valid: bool
    error_count: int  # total number of violations, even when ``errors`` is capped
    errors: tuple[StructuralError, ...]


def _load_validators() -> dict[str, Any]:
    schema_dir = resources.files("dc_odcs") / "schemas"
    manifest = json.loads((schema_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    validators: dict[str, Any] = {}
    for row in manifest:
        schema = json.loads((schema_dir / row["file"]).read_text(encoding="utf-8"))
        validators[row["api_version"]] = jsonschema_rs.validator_for(schema)
    return validators


_VALIDATORS = _load_validators()


def _pointer(path: Any) -> str:
    return "".join("/" + str(segment).replace("~", "~0").replace("/", "~1") for segment in path)


def _keyword(schema_path: Any) -> str:
    for segment in reversed(list(schema_path)):
        if isinstance(segment, str):
            return segment
    return "schema"


@functools.cache
def schema_manifest_sha256() -> str:
    """Hex sha256 of the vendored schema ``MANIFEST.json``: identifies the schema set (D-132)."""
    manifest = resources.files("dc_odcs") / "schemas" / "MANIFEST.json"
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


def clean_property_name(name: str) -> str:
    """A property key made safe to show: control and format characters dropped, backticks and
    runs of whitespace replaced by a single space, at most ``MAX_PROPERTY_NAME_CHARS`` long."""
    out: list[str] = []
    for char in name:
        category = unicodedata.category(char)
        if category in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            continue
        out.append(" " if char == "`" or category == "Zs" else char)
    cleaned = " ".join("".join(out).split())
    if len(cleaned) > MAX_PROPERTY_NAME_CHARS:
        return cleaned[: MAX_PROPERTY_NAME_CHARS - 3] + "..."
    return cleaned


def unknown_properties_message(names: Sequence[str], count: int | None = None) -> str:
    """The fixed sentence plus the offending keys: ``... allow: `a`, `b` and 3 more.``

    ``names`` are already cleaned (``StructuralError.unexpected``); ``count`` is the total number
    of unexpected keys (defaults to ``len(names)``). Without names the sentence stays as before.
    """
    base = "The object has properties the schema does not allow"
    shown = list(names[:MAX_NAMED_PROPERTIES])
    if not shown:
        return base + "."
    total = max(count if count is not None else len(shown), len(shown))
    listed = ", ".join(f"`{name}`" for name in shown)
    rest = total - len(shown)
    if rest > 0:
        return f"{base}: {listed} and {rest} more."
    return f"{base}: {listed}."


def _unexpected_names(kind: Any) -> tuple[tuple[str, ...], int]:
    raw: Iterable[Any] = getattr(kind, "unexpected", None) or ()
    names: list[str] = []
    total = 0
    for item in raw:
        total += 1
        if len(names) < MAX_NAMED_PROPERTIES:
            names.append(clean_property_name(str(item)))
    return tuple(names), total


def _bounded(message: str) -> str:
    if len(message) <= MAX_MESSAGE_CHARS:
        return message
    return message[: MAX_MESSAGE_CHARS - 3] + "..."


def validate_structure(document: Any, api_version: str) -> StructuralReport:
    """Validate ``document`` against the vendored schema for ``api_version``."""
    try:
        validator = _VALIDATORS[api_version]
    except KeyError:
        raise ValueError(f"No vendored ODCS schema for apiVersion {api_version!r}") from None
    errors: list[StructuralError] = []
    total = 0
    for error in validator.iter_errors(document):
        total += 1
        if len(errors) < MAX_REPORTED_ERRORS:
            keyword = _keyword(error.schema_path)
            if keyword in UNKNOWN_PROPERTY_KEYWORDS:
                names, name_count = _unexpected_names(error.kind)
                message = unknown_properties_message(names, name_count)
            else:
                names, name_count = (), 0
                message = _bounded(error.message)
            errors.append(
                StructuralError(
                    pointer=_pointer(error.instance_path),
                    message=message,
                    keyword=keyword,
                    unexpected=names,
                    unexpected_count=name_count,
                )
            )
    return StructuralReport(valid=total == 0, error_count=total, errors=tuple(errors))
