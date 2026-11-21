"""Request-body ingest: a streaming size cap, then parsing off the event loop (CONT-01, T-01-43).

The create route has no Pydantic body parameter. It authenticates first (the policy dependency
runs before the handler) and only then reads the raw bytes here, so an unauthenticated client
never makes the service buffer or parse anything (research Pitfall 1).

Per D-19 only ``ContractRejected`` (parse errors, media type, size, the apiVersion/kind/id gate)
blocks a create. A structural (JSON Schema) report with errors never raises: it is returned in
the 201 body.
"""

from __future__ import annotations

import asyncio
from typing import Any

# Moved to dc_core so every service shares it; the name stays importable from here.
from dc_core.body import read_body_capped as read_body_capped
from dc_core.names import disallowed_detail
from dc_core.problems import ProblemError
from dc_odcs import (
    ContractRejected,
    ParsedContract,
    StructuralReport,
    parse_contract,
    validate_structure,
)

from dc_contracts.settings import ContractsSettings


def rejection_to_problem(exc: ContractRejected) -> ProblemError:
    """Map the library's rejection 1:1 to a ``urn:dc:problem:<slug>`` problem."""
    extensions: dict[str, Any] = {}
    if exc.pointer is not None:
        extensions["errors"] = [{"pointer": exc.pointer, "detail": exc.detail}]
    return ProblemError(exc.status, exc.slug, exc.title, exc.detail, **extensions)


async def parse_upload(raw: bytes, media_type: str, settings: ContractsSettings) -> ParsedContract:
    """Parse and gate off the event loop (PyYAML/libyaml and json are CPU-bound)."""
    try:
        return await asyncio.to_thread(
            parse_contract,
            raw,
            media_type,
            supported=settings.supported_odcs_versions,
            max_bytes=settings.max_document_bytes,
        )
    except ContractRejected as exc:
        raise rejection_to_problem(exc) from exc


async def structural_report(parsed: ParsedContract) -> StructuralReport:
    """Non-blocking ODCS JSON Schema report for the document's own apiVersion (D-19)."""
    return await asyncio.to_thread(validate_structure, parsed.document, parsed.api_version)


def check_document_name(document: Any, current_name: str | None = None) -> None:
    """Refuse a top-level ``name`` that breaks the one name rule (quick 261120-lbz, D-01).

    Called after ``parse_upload`` succeeded and before any write; the stored bytes are never
    touched. A missing or non-string name is the ODCS schema's concern. ``current_name`` is the
    contract's stored name on a draft save: an unchanged name is never re-checked, so a legacy name
    does not block an unrelated save. Only the offending code point is named, never the character.
    """
    name = document.get("name") if isinstance(document, dict) else None
    if not isinstance(name, str) or name == current_name:
        return
    detail = disallowed_detail(name)
    if detail is not None:
        raise ProblemError(
            422,
            "validation-error",
            "Unprocessable Content",
            "The request failed validation.",
            errors=[{"pointer": "/name", "detail": detail}],
        )
