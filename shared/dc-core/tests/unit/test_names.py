"""The one name rule (quick 261120-lbz, D-01): allowed classes, refusals with U+XXXX, trimming."""

from __future__ import annotations

from typing import Annotated

import pytest
from dc_core.names import (
    NAME_RULE_HINT,
    NameRule,
    NameRuleError,
    check_name,
    code_point,
    first_disallowed,
)
from pydantic import BaseModel, StringConstraints, ValidationError

pytestmark = pytest.mark.unit

ACCEPTED = [
    "Ledger team",
    "Équipe données",
    "数据团队",
    "فريق البيانات",
    "צוות",
    "हिन्दी",
    "Team 42 (EU) & Co.",
    "a-b_c.d,e/f:g#h+i@j'k",
]

REFUSED = [
    ("😀 team", "U+1F600"),
    ("Team\u202eevil", "U+202E"),
    ("a\u2066b", "U+2066"),
    ("zero\u200bwidth", "U+200B"),
    ("\ufeffbom", "U+FEFF"),
    ("tab\there", "U+0009"),
    ("line\nbreak", "U+000A"),
    ("a b", "U+2028"),
    ("a b", "U+2029"),
    ("price $5", "U+0024"),
    ("a=b", "U+003D"),
    ("a^b", "U+005E"),
    ("©", "U+00A9"),
    ("", "U+E000"),
    ("a͸b", "U+0378"),
    ("1️⃣", "U+FE0F"),
    ("a b", "U+00A0"),
    ("O’Brien", "U+2019"),
    ("a\ud800b", "U+D800"),
    ("a\u200db", "U+200D"),
    ("a\u200cb", "U+200C"),
    ("a\U000e0100b", "U+E0100"),
    ("1⃣", "U+20E3"),
]


@pytest.mark.parametrize("value", ACCEPTED)
def test_accepts_letters_marks_digits_and_the_allowed_punctuation(value: str) -> None:
    assert first_disallowed(value) is None
    assert check_name(value) == value


@pytest.mark.parametrize(("value", "point"), REFUSED)
def test_refuses_with_the_code_point_and_never_the_raw_character(value: str, point: str) -> None:
    with pytest.raises(NameRuleError) as caught:
        check_name(value)
    detail = caught.value.detail
    assert point in detail
    assert NAME_RULE_HINT in detail
    assert not any(ch in detail for ch in value if not ch.isascii() or ch in "\t\n$^=")


def test_first_offending_character_is_reported() -> None:
    with pytest.raises(NameRuleError) as caught:
        check_name("a\u202eb\U0001f600")
    assert "U+202E" in caught.value.detail
    assert "U+1F600" not in caught.value.detail


def test_trims_regular_spaces_only() -> None:
    assert check_name("  Ledger  ") == "Ledger"
    with pytest.raises(NameRuleError):
        check_name("\tLedger")
    with pytest.raises(NameRuleError):
        check_name("Ledger\n")


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_is_refused(value: str) -> None:
    with pytest.raises(NameRuleError) as caught:
        check_name(value)
    assert caught.value.detail == "Must not be blank."


def test_code_point_has_at_least_four_upper_case_hex_digits() -> None:
    assert code_point("\t") == "U+0009"
    assert code_point("\u202e") == "U+202E"
    assert code_point("\U0001f600") == "U+1F600"


class Item(BaseModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=20), NameRule]


def test_model_trims_and_accepts() -> None:
    assert Item(name="  数据团队  ").name == "数据团队"


def test_model_error_message_is_bare_and_carries_no_raw_input() -> None:
    with pytest.raises(ValidationError) as caught:
        Item(name="Team\u202eevil")
    (error,) = caught.value.errors()
    assert error["msg"] == (
        "Contains U+202E, which is not allowed. "
        "Use letters, numbers, spaces and - _ . , ( ) & ' / : # + @ only."
    )
    assert not error.get("ctx")
    assert "\u202e" not in error["msg"]


def test_model_blank_message() -> None:
    with pytest.raises(ValidationError) as caught:
        Item(name="   ")
    assert caught.value.errors()[0]["msg"] == "Must not be blank."
