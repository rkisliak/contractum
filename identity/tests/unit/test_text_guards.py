"""``reject_nul`` and ``reject_control``: the text guards behind identity's request models."""

from __future__ import annotations

import pytest
from dc_identity.text import NoNulStr, reject_control, reject_nul
from pydantic import TypeAdapter, ValidationError

pytestmark = pytest.mark.unit


def test_reject_nul_returns_a_clean_value_unchanged() -> None:
    assert reject_nul("plain text") == "plain text"


def test_reject_nul_refuses_a_nul_character() -> None:
    with pytest.raises(ValueError, match="NUL"):
        reject_nul("a\x00b")


def test_the_no_nul_string_type_is_a_validation_error_not_a_database_error() -> None:
    adapter = TypeAdapter(NoNulStr)
    assert adapter.validate_python("fine") == "fine"
    with pytest.raises(ValidationError, match="NUL"):
        adapter.validate_python("bad\x00")


@pytest.mark.parametrize("value", ["", "Ana Maria", "Zoë", "zero‍width joiner", "a-b_c.d"])
def test_reject_control_accepts_printable_text(value: str) -> None:
    assert reject_control(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "tab\there",
        "new\nline",
        "del\x7f",
        "c1\x85",
        "line\u2028separator",
        "paragraph\u2029separator",
        "\u202eoverride",
        "isolate\u2066",
    ],
)
def test_reject_control_refuses_control_separator_and_bidi_characters(value: str) -> None:
    with pytest.raises(ValueError, match="control or bidirectional"):
        reject_control(value)
