"""Search-field extraction for contract list/search (D-12). Total: never raises on odd shapes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SearchFields:
    name: str | None
    domain: str | None
    status: str | None
    tags: tuple[str, ...]
    description_text: str | None
    odcs_version: str | None  # the contract's own ``version`` field (a string, e.g. "1.0.0")


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def extract_search_fields(document: dict[str, Any]) -> SearchFields:
    raw_tags = document.get("tags")
    tags = (
        tuple(tag for tag in raw_tags if isinstance(tag, str)) if isinstance(raw_tags, list) else ()
    )
    description = document.get("description")
    description_text: str | None = None
    if isinstance(description, dict):
        parts = [
            part
            for key in ("purpose", "usage", "limitations")
            if isinstance(part := description.get(key), str) and part
        ]
        description_text = "\n".join(parts) if parts else None
    return SearchFields(
        name=_text(document.get("name")),
        domain=_text(document.get("domain")),
        status=_text(document.get("status")),
        tags=tags,
        description_text=description_text,
        odcs_version=_text(document.get("version")),
    )
