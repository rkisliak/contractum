"""The mail service wire contract (D-105): narrow request, fixed messages, never an echo."""

from __future__ import annotations

from typing import Any

import pytest
from dc_core.mail import (
    MAX_ADDRESS_CHARS,
    MAX_MAIL_RECIPIENTS,
    MAX_SUBJECT_CHARS,
    MAX_TEXT_CHARS,
    MailSendIn,
    MailSendOut,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit

GOOD = {"to": ["ops@example.com"], "subject": "Contract published", "text": "Hello\r\n\tthere"}


def _send(**overrides: Any) -> MailSendIn:
    return MailSendIn.model_validate({**GOOD, **overrides})


def _messages(caught: pytest.ExceptionInfo[ValidationError]) -> list[str]:
    return [error["msg"] for error in caught.value.errors()]


def test_a_plain_request_is_accepted_with_tab_and_newlines_in_the_text() -> None:
    sent = _send(to=["a.b+tag@mail.example.co", "x@sub-domain.example.org"])
    assert sent.to == ["a.b+tag@mail.example.co", "x@sub-domain.example.org"]
    assert sent.text == "Hello\r\n\tthere"


@pytest.mark.parametrize(
    "address",
    [
        "no-at-sign",
        "two@@example.com",
        "dot.@example.com",
        "user@localhost",
        "user@-bad.example.com",
        "us er@example.com",
        "user@example.com\n",
        "user\x00@example.com",
        "a" * 65 + "@example.com",
        "a@" + "b" * (MAX_ADDRESS_CHARS - 6) + ".com",
    ],
)
def test_an_invalid_recipient_is_refused_with_a_fixed_message(address: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _send(to=[address])
    assert _messages(caught) == ["Value error, Not a valid email address."]
    assert address not in str(caught.value.errors(include_input=False))


@pytest.mark.parametrize("subject", ["Line\nbreak", "Tab\there", "Bell\x07", "Del\x7f"])
def test_a_subject_with_a_control_character_is_refused(subject: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _send(subject=subject)
    assert _messages(caught) == ["Value error, The subject must not contain control characters."]


@pytest.mark.parametrize("text", ["nul\x00", "vt\x0b", "esc\x1b", "del\x7f"])
def test_a_text_with_a_disallowed_control_character_is_refused(text: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _send(text=text)
    assert _messages(caught) == ["Value error, The text must not contain control characters."]


@pytest.mark.parametrize(
    "overrides",
    [
        {"to": []},
        {"to": [f"u{n}@example.com" for n in range(MAX_MAIL_RECIPIENTS + 1)]},
        {"subject": ""},
        {"subject": "s" * (MAX_SUBJECT_CHARS + 1)},
        {"text": ""},
        {"text": "t" * (MAX_TEXT_CHARS + 1)},
        {"sender": "boss@example.com"},
        {"bcc": ["spy@example.com"]},
    ],
)
def test_bounds_and_unknown_fields_are_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _send(**overrides)


def test_the_answer_defaults_deferred_to_zero_for_an_older_service() -> None:
    assert MailSendOut.model_validate({"accepted": 2, "refused": 1}).deferred == 0
    answer = MailSendOut(accepted=0, refused=3, deferred=2)
    assert answer.model_dump() == {"accepted": 0, "refused": 3, "deferred": 2}
