"""Notation adapters: the seam between the lifecycle and a contract notation (D-41).

The lifecycle (publish, semver ordering, fetch) is notation-neutral; what is notation-specific is
how the version a document declares is read, and how a derived copy changes the fields that make
it a new version. ODCS is the only notation today. ``Contract.notation`` is server-stored (never
client input), so an unknown value is a programming error, not a 4xx.

``starter`` builds a brand-new document on the server (D-43): a dictionary serialised with
``yaml.safe_dump``, never YAML source with caller text interpolated into it.

``copy_with`` is a fidelity-preserving in-place edit (CONT-06, contract-ingest rule 4): only the
value token of a top-level scalar changes, located in the stored bytes, and a strict re-parse
proves nothing else did. A shape that cannot be edited in place is refused, never rewritten.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

import yaml
from dc_core.problems import ProblemError
from dc_odcs import ContractRef, extract_contract_refs, load_json, load_yaml

from dc_contracts.convert import _BOM, JSON_TYPE, YAML_FAMILY

_JSON_WHITESPACE = " \t\n\r"
_PLAIN_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_FIELD_REMEDY = {
    "version": "create the draft with bump none and edit the version yourself",
    "id": "download the document and create the contract from it with the full-document route",
}
# A wider width than any accepted document, so the emitter never folds a scalar over lines.
_NO_FOLD = 10**9
_YAML_TYPE = "application/yaml"
_STARTER_API_VERSION = "v3.2.0"
_STARTER_VERSION = "0.1.0"


class NotationAdapter(Protocol):
    notation: str

    def declared_version(self, document: dict[str, Any]) -> str | None:
        """The version string the document declares, or None when it is absent or not a string."""
        ...

    def references(self, document: dict[str, Any]) -> list[ContractRef]:
        """The links from this document to other contracts (CONT-04, D-54); bounded and total."""
        ...

    def copy_with(
        self, raw: bytes, media_type: str, *, odcs_id: str | None, version: str | None
    ) -> bytes:
        """The stored bytes with only the named top-level scalars replaced (sync, CPU-bound)."""
        ...

    def starter(
        self, *, odcs_id: str, name: str, api_version: str | None = None
    ) -> tuple[bytes, str]:
        """A new server-generated starter document and its media type (sync, CPU-bound)."""
        ...


def _not_editable(field: str) -> ProblemError:
    return ProblemError(
        422,
        "copy-field-not-editable",
        "Unprocessable Content",
        f"The source document's top-level {field} is not a single-line scalar, so it cannot be "
        f"changed without rewriting the document; {_FIELD_REMEDY[field]}.",
    )


def _invalid_value() -> ProblemError:
    return ProblemError(
        422,
        "validation-error",
        "Unprocessable Content",
        "The new value cannot be encoded as UTF-8 text.",
    )


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _yaml_spans(text: str, fields: list[str]) -> dict[str, tuple[int, int, str]]:
    """``field -> (start, end, style)`` of each root-level single-line scalar value."""
    try:
        root = yaml.compose(text, Loader=yaml.CSafeLoader)
    except yaml.YAMLError:
        raise _not_editable(fields[0]) from None
    if not isinstance(root, yaml.MappingNode):
        raise _not_editable(fields[0])
    found: dict[str, tuple[int, int, str]] = {}
    for key, value in root.value:
        if not isinstance(key, yaml.ScalarNode) or key.value not in fields:
            continue
        field = key.value
        if (
            not isinstance(value, yaml.ScalarNode)
            or value.style not in ("", "'", '"')
            or value.start_mark.line != value.end_mark.line
        ):
            raise _not_editable(field)
        start, end = value.start_mark.index, value.end_mark.index
        token = text[start:end]
        # A tagged plain scalar's span starts at its tag, so the token differs from the value.
        if value.style == "" and token != value.value:
            raise _not_editable(field)
        found[field] = (start, end, value.style)
    for field in fields:
        if field not in found:
            raise _not_editable(field)
    return found


def _skip_json_space(text: str, index: int) -> int:
    while index < len(text) and text[index] in _JSON_WHITESPACE:
        index += 1
    return index


def _json_spans(text: str, fields: list[str]) -> dict[str, tuple[int, int, str]]:
    """``field -> (start, end, '"')`` of each root-level string value, by walking the object."""
    index = _skip_json_space(text, 0)
    if index >= len(text) or text[index] != "{":
        raise _not_editable(fields[0])
    index += 1
    decoder = json.JSONDecoder()
    found: dict[str, tuple[int, int, str]] = {}
    try:
        while True:
            index = _skip_json_space(text, index)
            if text[index] == "}":
                break
            if text[index] == ",":
                index = _skip_json_space(text, index + 1)
            if text[index] != '"':
                raise ValueError("expected a member name")
            # Decoding starts at a quote, so the member name is always a str.
            key, index = decoder.raw_decode(text, index)
            index = _skip_json_space(text, index)
            if text[index] != ":":
                raise ValueError("expected a colon")
            index = _skip_json_space(text, index + 1)
            value, end = decoder.raw_decode(text, index)
            if key in fields and key not in found:
                if not isinstance(value, str) or text[index] != '"':
                    raise _not_editable(key)
                found[key] = (index, end, '"')
            index = end
    except (ValueError, IndexError):
        raise _not_editable(fields[0]) from None
    for field in fields:
        if field not in found:
            raise _not_editable(field)
    return found


def _strict_load(text: str) -> object:
    try:
        return load_yaml(text)
    except (ValueError, yaml.YAMLError):
        return None


def _yaml_token(field: str, value: str, style: str) -> str:
    # A bumped version is digits and dots: it needs no escaping, so the source's quoting is kept.
    if field == "version" and _PLAIN_VERSION.match(value):
        return {"": value, "'": f"'{value}'", '"': f'"{value}"'}[style]
    emitted = str(yaml.safe_dump(value, default_style='"', allow_unicode=True, width=_NO_FOLD))
    return emitted.rstrip("\n")


class OdcsAdapter:
    notation = "odcs"

    def declared_version(self, document: dict[str, Any]) -> str | None:
        # Never ``str()`` a number: ``version: 1.10`` must not silently become "1.1" (research
        # Pitfall 5). A non-string is None and the publish refuses it as invalid-version.
        value = document.get("version")
        return value if isinstance(value, str) else None

    def references(self, document: dict[str, Any]) -> list[ContractRef]:
        # Which ODCS fields count as a link to another contract is ODCS-specific, so it stays
        # behind the adapter seam (D-41); the rules themselves live in dc_odcs.refs.
        return extract_contract_refs(document)

    def starter(
        self, *, odcs_id: str, name: str, api_version: str | None = None
    ) -> tuple[bytes, str]:
        """The ODCS starter for a new contract: six keys, ``yaml.safe_dump`` of a dictionary (D-43).

        ``api_version`` is the effective default ODCS apiVersion (quick 261120-0ye, G2: a stored
        governance setting, else ``DC_DEFAULT_ODCS_API_VERSION``); the caller has already checked it
        against the supported set. ``None`` keeps the built-in ``v3.2.0``.

        Caller text is only ever a dictionary value, so a newline, a colon or ``#`` in the name
        stays inside that one string and cannot add or override a key (T-03-26). The result is
        proven to strict-load back to exactly the dictionary; a plain scalar the strict loader
        would read as another type is dumped with every scalar double-quoted instead.
        """
        document = {
            "apiVersion": api_version or _STARTER_API_VERSION,
            "kind": "DataContract",
            "id": odcs_id,
            "name": name,
            "version": _STARTER_VERSION,
            "status": "draft",
        }
        for value in (odcs_id, name):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise _invalid_value() from None
        options: dict[str, Any] = {"sort_keys": False, "allow_unicode": True, "width": _NO_FOLD}
        text = str(yaml.safe_dump(document, **options))
        if _strict_load(text) != document:
            text = str(yaml.safe_dump(document, default_style='"', **options))
            if _strict_load(text) != document:  # pragma: no cover - defensive, never observed
                raise _invalid_value()
        return text.encode("utf-8"), _YAML_TYPE

    def copy_with(
        self, raw: bytes, media_type: str, *, odcs_id: str | None, version: str | None
    ) -> bytes:
        """Replace the top-level ``id`` / ``version`` scalar tokens in place (CONT-06, T-03-15).

        Comments, key order, quoting, line endings, a leading BOM and the media type are kept. The
        result is proven by a strict re-parse to equal the source document with only the named
        fields changed; any shape that cannot be edited that way is a 422
        ``copy-field-not-editable``. Nothing is ever re-serialised.
        """
        wanted = {"id": odcs_id, "version": version}
        is_json = _media_type(media_type) == JSON_TYPE
        if not is_json and _media_type(media_type) not in YAML_FAMILY:
            raise RuntimeError(f"unsupported media type {media_type!r}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise _not_editable("version") from None
        bom = text.startswith(_BOM)
        if bom:
            text = text[1:]
        loader = load_json if is_json else load_yaml
        try:
            source = loader(text)
        except (ValueError, yaml.YAMLError):
            raise _not_editable("version") from None
        if not isinstance(source, dict):
            raise _not_editable("version")

        changes = {
            field: value
            for field, value in wanted.items()
            if value is not None and source.get(field) != value
        }
        if not changes:
            return raw
        for value in changes.values():
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise _invalid_value() from None

        fields = list(changes)
        spans = (_json_spans if is_json else _yaml_spans)(text, fields)
        edited = text
        for field in sorted(fields, key=lambda name: spans[name][0], reverse=True):
            start, end, style = spans[field]
            token = (
                json.dumps(changes[field], ensure_ascii=False)
                if is_json
                else _yaml_token(field, changes[field], style)
            )
            edited = edited[:start] + token + edited[end:]

        try:
            result = loader(edited)
        except (ValueError, yaml.YAMLError):
            raise _not_editable(fields[0]) from None
        if result != {**source, **changes}:
            raise _not_editable(fields[0])
        return ((_BOM if bom else "") + edited).encode("utf-8")


_ODCS = OdcsAdapter()


def get_adapter(notation: str) -> NotationAdapter:
    if notation == "odcs":
        return _ODCS
    raise RuntimeError(f"no notation adapter for {notation!r}")


__all__ = ["NotationAdapter", "OdcsAdapter", "get_adapter"]
