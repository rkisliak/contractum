"""The ``sort=name`` cursor codec and the id cursor golden (D-07, T-16-08, no database)."""

from __future__ import annotations

import base64
import uuid

import pytest
from dc_contracts.search import (
    MAX_CURSOR_CHARS,
    NAME_KEY_CHARS,
    decode_name_cursor,
    encode_cursor,
    encode_name_cursor,
)
from dc_core.problems import ProblemError

pytestmark = pytest.mark.unit

CONTRACT_ID = uuid.UUID("01890000-0000-7000-8000-000000000001")
# The id cursor as computed by the implementation before Phase 16: base64url of the uuid text.
ID_CURSOR_GOLDEN = "MDE4OTAwMDAtMDAwMC03MDAwLTgwMDAtMDAwMDAwMDAwMDAx"


def _raw(payload: bytes) -> str:
    """A cursor spelling of arbitrary bytes, canonical as base64url without padding."""
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _name_payload(key: bytes, contract_id: uuid.UUID = CONTRACT_ID) -> bytes:
    return b"n:" + str(contract_id).encode() + b":" + key


def _assert_rejected(raw: str) -> None:
    with pytest.raises(ProblemError) as caught:
        decode_name_cursor(raw)
    assert caught.value.status == 422
    assert caught.value.extensions["errors"] == [
        {"pointer": "/query/cursor", "detail": "cursor is not valid"}
    ]


@pytest.mark.parametrize(
    "key",
    [
        "alpha",
        "",
        "Ünïcode",
        "مرحبا بالعالم",
        "契約データ",
        "emoji 😀🚀",
        "a:b:c",
        "x" * NAME_KEY_CHARS,
    ],
)
def test_the_name_cursor_round_trips_every_script(key: str) -> None:
    raw = encode_name_cursor(key, CONTRACT_ID)
    assert "=" not in raw
    assert decode_name_cursor(raw) == (key, CONTRACT_ID)


def test_only_the_canonical_spelling_decodes() -> None:
    raw = encode_name_cursor("alpha", CONTRACT_ID)
    padding = "=" * (-len(raw) % 4)
    if padding:
        _assert_rejected(raw + padding)
    else:
        _assert_rejected(raw + "=")
    standard = base64.b64encode(_name_payload(b"alpha??>>")).decode("ascii").rstrip("=")
    assert "+" in standard or "/" in standard
    _assert_rejected(standard)
    _assert_rejected(raw.upper() if raw.upper() != raw else raw.lower())
    _assert_rejected(raw + "!")


def test_a_non_hexadecimal_or_upper_case_uuid_is_not_canonical() -> None:
    lettered = uuid.UUID("01890abc-def0-7000-8000-00000000abcd")
    assert decode_name_cursor(encode_name_cursor("k", lettered)) == ("k", lettered)
    _assert_rejected(_raw(b"n:" + str(lettered).upper().encode() + b":k"))
    _assert_rejected(_raw(b"n:" + lettered.hex.encode() + b":k"))


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not-a-cursor",
        _raw(b"n:"),
        _raw(b"x:" + str(CONTRACT_ID).encode() + b":k"),
        _raw(b"n:" + str(CONTRACT_ID).encode()),
        _raw(b"n:" + str(CONTRACT_ID).encode() + b"-k"),
        _raw(b"n:not-a-uuid-not-a-uuid-not-a-uuid-xx:k"),
    ],
)
def test_malformed_name_cursors_are_rejected(raw: str) -> None:
    _assert_rejected(raw)


def test_the_id_cursor_is_not_a_name_cursor() -> None:
    _assert_rejected(encode_cursor(CONTRACT_ID))


def test_a_key_with_nul_is_rejected() -> None:
    _assert_rejected(_raw(_name_payload(b"ab\x00cd")))
    _assert_rejected(encode_name_cursor("ab\x00cd", CONTRACT_ID))


def test_a_lone_surrogate_in_the_bytes_is_rejected() -> None:
    _assert_rejected(_raw(_name_payload(b"\xed\xa0\x80")))
    _assert_rejected(_raw(_name_payload(b"\xff\xfe")))


def test_a_key_over_the_bound_is_rejected() -> None:
    _assert_rejected(encode_name_cursor("a" * (NAME_KEY_CHARS + 1), CONTRACT_ID))
    assert decode_name_cursor(encode_name_cursor("a" * NAME_KEY_CHARS, CONTRACT_ID))[0] == (
        "a" * NAME_KEY_CHARS
    )


def test_the_raw_length_cap_applies_before_any_decoding() -> None:
    _assert_rejected("A" * (MAX_CURSOR_CHARS + 1))
    _assert_rejected("!" * (MAX_CURSOR_CHARS + 1))


def test_the_id_cursor_is_byte_identical_to_before_phase_16() -> None:
    assert encode_cursor(CONTRACT_ID) == ID_CURSOR_GOLDEN
