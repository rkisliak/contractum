"""Wire contract of the mail service (D-105), shared by the service and its callers.

The mail service turns "send this text to these addresses" into one SMTP message. The request
model is deliberately narrow: no sender, no Bcc, no headers, plain text only. Every validator
raises a fixed message, so a rejected request never echoes what was submitted (the platform's 422
body carries only the location and the validator message).
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

MAIL_SEND_PATH = "/internal/v1/mail/send"
MAX_MAIL_RECIPIENTS = 20
MAX_MAIL_BODY_BYTES = 65536
MAX_ADDRESS_CHARS = 320
MAX_SUBJECT_CHARS = 200
MAX_TEXT_CHARS = 20000

_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
# Local part 1..64 characters (dot-separated atoms, no quoting, no whitespace), then a dotted
# domain of at least two letter/digit/hyphen labels. The 320 character total is checked separately.
EMAIL_ADDRESS_RE = re.compile(rf"(?=[^@]{{1,64}}@){_ATOM}(?:\.{_ATOM})*@{_LABEL}(?:\.{_LABEL})+")

# C0 controls and DEL. Tab, CR and LF are allowed in the text only.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_TEXT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _check_address(value: str) -> str:
    if (
        len(value) > MAX_ADDRESS_CHARS
        or _CONTROL_RE.search(value)
        or EMAIL_ADDRESS_RE.fullmatch(value) is None
    ):
        raise ValueError("Not a valid email address.")
    return value


class MailSendIn(BaseModel):
    """What a caller may ask for: recipients, a subject and a plain-text body. Nothing else."""

    model_config = ConfigDict(extra="forbid")

    to: Annotated[list[str], Field(min_length=1, max_length=MAX_MAIL_RECIPIENTS)]
    subject: Annotated[str, StringConstraints(min_length=1, max_length=MAX_SUBJECT_CHARS)]
    text: Annotated[str, StringConstraints(min_length=1, max_length=MAX_TEXT_CHARS)]

    @field_validator("to")
    @classmethod
    def _addresses(cls, value: list[str]) -> list[str]:
        for address in value:
            _check_address(address)
        return value

    @field_validator("subject")
    @classmethod
    def _subject(cls, value: str) -> str:
        if _CONTROL_RE.search(value):
            raise ValueError("The subject must not contain control characters.")
        return value

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        if _TEXT_CONTROL_RE.search(value):
            raise ValueError("The text must not contain control characters.")
        return value


class MailSendOut(BaseModel):
    """Recipients the SMTP server took and recipients it refused (a partial send is still 200).

    ``refused`` counts every refusal; ``deferred`` is the temporary (4xx) part of it.
    """

    accepted: int
    refused: int
    # Of the refused, how many were temporary (SMTP 4xx: greylisting, mailbox full). A caller that
    # retries should treat these as not delivered. Defaults to 0 so an older answer still parses.
    deferred: int = 0
