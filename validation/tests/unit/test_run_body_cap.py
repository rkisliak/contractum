"""The internal run body cap leaves room for ``ref_facts`` (IN-02).

The cap used to cover the two base64 documents and a 64 KiB envelope only, so a near-maximal
document and baseline with many long links was a 413, which contracts reports as a 503. A request
built the way contracts builds it (``json.dumps``, ASCII escapes) must always fit.
"""

from __future__ import annotations

import base64
import json
import uuid

import pytest
from dc_odcs import MAX_VERSION_CHARS
from dc_validation.api.runs import run_body_cap
from dc_validation.facts import MAX_LINK_FACTS, MAX_LINK_KEY_CHARS
from dc_validation.settings import ValidationSettings

pytestmark = pytest.mark.unit


def test_a_maximal_request_with_maximal_link_facts_fits_the_cap() -> None:
    settings = ValidationSettings(database_url="postgresql+psycopg://x@localhost/x")  # type: ignore[arg-type]
    document = base64.b64encode(b"x" * settings.max_document_bytes).decode("ascii")
    # Worst-case keys: non-BMP characters escape to 12 bytes each in ASCII JSON.
    facts = {
        f"{i:04d}" + "\U0001f600" * (MAX_LINK_KEY_CHARS - 4): {
            "state": "unpublished_or_draft_only",
            "pinned_version": "1" * MAX_VERSION_CHARS,
            "pinned_published": False,
        }
        for i in range(MAX_LINK_FACTS)
    }
    payload = {
        "mode": "publish",
        "contract_id": str(uuid.uuid4()),
        "draft_id": str(uuid.uuid4()),
        "media_type": "application/yaml",
        "document_b64": document,
        "attachments": [],
        "attachments_revision": 0,
        "baseline": {
            "version_id": str(uuid.uuid4()),
            "semver": "1.0.0+" + "b" * (MAX_VERSION_CHARS - 6),
            "media_type": "application/yaml",
            "document_b64": document,
        },
        "stable_major_floor": 1,
        "ref_facts": {"contracts": facts},
    }

    body = json.dumps(payload).encode()

    assert len(body) <= run_body_cap(settings)
    # The old cap (documents plus 64 KiB) refused it.
    assert len(body) > 2 * len(document) + 64 * 1024
