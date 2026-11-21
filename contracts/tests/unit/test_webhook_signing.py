"""The Standard Webhooks signer and the webhook body (SUB-07, D-98, T-04.1-23).

The known-answer vector is computed independently of ``webhooks.sign`` so a mistake in the signed
string (id, timestamp or body order) fails here and not at a consumer's verifier.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from dc_contracts import webhooks

pytestmark = pytest.mark.unit

RAW_KEY = b"\x01" * 32
SECRET = "whsec_" + base64.b64encode(RAW_KEY).decode()
OTHER_RAW_KEY = b"\x02" * 32
OTHER_SECRET = "whsec_" + base64.b64encode(OTHER_RAW_KEY).decode()
BODY = b'{"a":1}'


def expected(raw_key: bytes, msg_id: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(raw_key, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def make_notice(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "id": uuid.UUID("00000000-0000-7000-8000-000000000001"),
        "card_id": uuid.UUID("00000000-0000-7000-8000-000000000002"),
        "event_type": "contract.version.deprecated",
        "occurred_at": datetime(2026, 11, 13, 10, 30, 5, tzinfo=UTC),
        "payload": {
            "contract": {"id": "c-1", "odcs_id": "urn:x"},
            "version": {"id": "v-1", "semver": "2.3.0"},
            "sunset_date": "2027-01-01",
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_a_generated_secret_is_whsec_and_decodes_to_32_bytes() -> None:
    secret = webhooks.generate_secret()
    assert secret.startswith("whsec_")
    assert len(base64.b64decode(secret.removeprefix("whsec_"), validate=True)) == 32


def test_the_known_answer_vector_matches_an_independent_hmac() -> None:
    signature = webhooks.sign(SECRET, "msg_1", 1700000000, BODY)
    assert signature == expected(RAW_KEY, "msg_1", 1700000000, BODY)
    assert signature.startswith("v1,")


def test_the_signature_depends_on_id_timestamp_body_and_key() -> None:
    base = webhooks.sign(SECRET, "msg_1", 1700000000, BODY)
    assert webhooks.sign(SECRET, "msg_2", 1700000000, BODY) != base
    assert webhooks.sign(SECRET, "msg_1", 1700000001, BODY) != base
    assert webhooks.sign(SECRET, "msg_1", 1700000000, b'{"a":2}') != base
    assert webhooks.sign(OTHER_SECRET, "msg_1", 1700000000, BODY) != base


def test_the_header_has_one_space_separated_entry_per_secret_newest_first() -> None:
    header = webhooks.signature_header([SECRET, OTHER_SECRET], "msg_1", 1700000000, BODY)
    entries = header.split(" ")
    assert entries == [
        expected(RAW_KEY, "msg_1", 1700000000, BODY),
        expected(OTHER_RAW_KEY, "msg_1", 1700000000, BODY),
    ]


def test_a_single_secret_gives_a_single_entry() -> None:
    header = webhooks.signature_header([SECRET], "msg_1", 1700000000, BODY)
    assert " " not in header
    assert header == expected(RAW_KEY, "msg_1", 1700000000, BODY)


def test_the_body_is_deterministic_sorted_and_compact() -> None:
    notice = make_notice()
    first = webhooks.build_body(notice, "orders consumers")
    second = webhooks.build_body(notice, "orders consumers")
    assert first == second
    text = first.decode()
    assert ", " not in text
    assert ": " not in text
    parsed = json.loads(text)
    assert text == json.dumps(parsed, sort_keys=True, separators=(",", ":"))


def test_the_body_is_the_standard_webhooks_envelope() -> None:
    notice = make_notice()
    parsed = json.loads(webhooks.build_body(notice, "orders consumers"))
    assert set(parsed) == {"type", "timestamp", "data"}
    assert parsed["type"] == "contract.version.deprecated"
    assert datetime.fromisoformat(parsed["timestamp"]) == notice.occurred_at
    data = parsed["data"]
    assert data["schema_version"] == 1
    assert data["notice_id"] == str(notice.id)
    assert data["card"] == {"id": str(notice.card_id), "name": "orders consumers"}
    assert data["version"]["semver"] == "2.3.0"
    assert data["contract"]["odcs_id"] == "urn:x"


def test_the_timestamp_is_the_event_time_in_utc() -> None:
    zone = timezone(timedelta(hours=2))
    notice = make_notice(occurred_at=datetime(2026, 11, 13, 12, 30, 5, tzinfo=zone))
    parsed = json.loads(webhooks.build_body(notice, "c"))
    stamp = datetime.fromisoformat(parsed["timestamp"])
    assert stamp == datetime(2026, 11, 13, 10, 30, 5, tzinfo=UTC)
    assert stamp.utcoffset() == timedelta(0)


def test_a_naive_event_time_is_read_as_utc() -> None:
    notice = make_notice(occurred_at=datetime(2026, 11, 13, 10, 30, 5))  # noqa: DTZ001
    parsed = json.loads(webhooks.build_body(notice, "c"))
    assert parsed["timestamp"] == "2026-11-13T10:30:05Z"


def test_the_body_never_carries_contacts_members_or_other_cards() -> None:
    parsed = json.loads(webhooks.build_body(make_notice(), "c"))
    flat = json.dumps(parsed)
    for forbidden in ("contacts", "members", "email", "webhook_url", "whsec_"):
        assert forbidden not in flat


def test_the_fixed_envelope_fields_cannot_be_overridden_by_the_payload() -> None:
    notice = make_notice(payload={"schema_version": 99, "notice_id": "x", "card": {"id": "y"}})
    data = json.loads(webhooks.build_body(notice, "c"))["data"]
    assert data["schema_version"] == 1
    assert data["notice_id"] == str(notice.id)
    assert data["card"]["name"] == "c"
