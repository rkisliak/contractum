"""Parse a submitted document for a validation run, with value-free problem details (VAL-07).

Validation re-parses the raw bytes itself (D-125), with the same ``dc_odcs`` pipeline contracts
uses. The difference is the problem text: a ``dc_odcs`` rejection detail can quote the document (the
``unsupported-odcs-version`` detail embeds the declared ``apiVersion``, YAML parse errors can quote
the input), and a validation problem may be read by a principal who must not see document values.
So the status, slug, title and JSON Pointer of a rejection are kept, and the detail is replaced by
a fixed text per slug; the rejection's own detail is never copied.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from dc_core.problems import ProblemError
from dc_odcs import SUPPORTED_API_VERSIONS, ContractRejected, ParsedContract, parse_contract

from dc_validation.settings import ValidationSettings

FIXED_DETAILS: Mapping[str, str] = {
    "unsupported-odcs-version": "The document's apiVersion is not a supported ODCS version.",
    "malformed-document": "The document is not valid YAML or JSON.",
    "unsupported-media-type": "Use application/yaml or application/json.",
    "payload-too-large": "The document exceeds the size limit.",
    "validation-error": "The document fails the ODCS acceptance check.",
}

# A slug dc_odcs might add later gets this text, never the library's detail.
GENERIC_DETAIL = "The document was rejected."


def rejection_to_problem(exc: ContractRejected) -> ProblemError:
    """Status, slug, title and pointer from the rejection; the detail from ``FIXED_DETAILS``."""
    detail = FIXED_DETAILS.get(exc.slug, GENERIC_DETAIL)
    extensions: dict[str, Any] = {}
    if exc.pointer is not None:
        extensions["errors"] = [{"pointer": exc.pointer, "detail": detail}]
    return ProblemError(exc.status, exc.slug, exc.title, detail, **extensions)


async def parse_document(
    raw: bytes, media_type: str, settings: ValidationSettings
) -> ParsedContract:
    """Parse and gate off the event loop (PyYAML and json are CPU-bound)."""
    try:
        return await asyncio.to_thread(
            parse_contract,
            raw,
            media_type,
            supported=SUPPORTED_API_VERSIONS,
            max_bytes=settings.max_document_bytes,
        )
    except ContractRejected as exc:
        raise rejection_to_problem(exc) from exc
