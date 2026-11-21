"""The email channel's client side (SUB-07, D-105): render a notice, hand it to the mail service.

Contracts never speaks SMTP. A delivery becomes one POST to the mail service
(``DC_MAIL_URL`` + ``MAIL_SEND_PATH``) with the notifier service account's key, and the request body
is the shared ``dc_core.mail.MailSendIn``. The mail service never retries: every outcome here maps
to ``delivered`` or to a failed attempt that the worker retries on its own schedule (D-99).

Subjects and bodies are templates (the defaults, or the Global admin's stored override, rendered by
plain placeholder substitution: ``dc_core.platform_settings.render_template``); every value that
reaches them is stripped of control characters first and the rendered subject is sanitised again, so
a hostile ODCS id or card name cannot inject a header (T-04.1-31, T-0yd-02). The
body is built from the notice payload allow-list only: it never carries document content, card
contacts, card links or another card's data. The key, the addresses and the body are never logged
and never end up in an error code (T-04.1-41).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from dc_core.mail import (
    MAIL_SEND_PATH,
    MAX_SUBJECT_CHARS,
    MAX_TEXT_CHARS,
    MailSendIn,
    MailSendOut,
)
from dc_core.platform_settings import (
    DEFAULT_EMAIL_BODY,
    DEFAULT_EMAIL_SUBJECTS,
    TemplateError,
    render_template,
)
from pydantic import ValidationError

from dc_contracts.webhooks import NoticeLike

if TYPE_CHECKING:
    from dc_contracts.settings import ContractsSettings

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")

_GENERIC_EVENT = "generic"
# A subject piece taken from contract data (the ODCS id) is cut to this many characters.
ODCS_ID_SUBJECT_LIMIT = 80
_FIELD_SUBJECT_LIMIT = 40
_BODY_VALUE_LIMIT = 500
_UNKNOWN = "unknown"
# The breaking-change list is bounded three ways (D-164, T-6-37): lines in total, characters per
# line, and characters in the whole block (which keeps the message under the mail service's cap).
MAX_EMAIL_CHANGES = 50
_CHANGE_LINE_LIMIT = 300
_BLOCK_CHAR_BUDGET = 10_000
_BREAKING = "contract.version.breaking_published"

# One subject template per notice type (D-100) and the default body live in
# ``dc_core.platform_settings`` (quick 261120-0yd, D-20): the Global admin can override them, and
# the defaults are byte for byte what this module rendered before (the golden test pins it).
EMAIL_SUBJECTS: Mapping[str, str] = {
    event: subject for event, subject in DEFAULT_EMAIL_SUBJECTS.items() if event != _GENERIC_EVENT
}

# Event fields shown in the body, in order: (payload key, label).
_EVENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("status", "Status"),
    ("sunset_date", "Sunset date"),
    ("lead_days", "Days until sunset"),
    ("deprecated_at", "Deprecated at"),
    ("reason", "Reason"),
)


def sanitize_header_text(value: str, limit: int) -> str:
    """Collapse whitespace runs to one space, drop other C0 controls and DEL, cut to ``limit``."""
    # Whitespace controls (CR, LF, tab) become one space so words never run together; the other
    # C0 controls and DEL vanish.
    cleaned = _CONTROL_RE.sub("", _WHITESPACE_RE.sub(" ", str(value))).strip()
    return cleaned[:limit].rstrip()


def _scalar(value: Any) -> str | None:
    """A payload value as text; only plain scalars are rendered (never nested data)."""
    if value is None or isinstance(value, bool | dict | list | tuple):
        return None
    return str(value)


def _pick(mapping: Any, key: str) -> str | None:
    return _scalar(mapping.get(key)) if isinstance(mapping, Mapping) else None


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _value_text(value: Any) -> str:
    """A before or after value as text: a scalar, or a short list of scalars; never nested data."""
    if isinstance(value, list):
        return "[" + ", ".join(_scalar(item) or "-" for item in value[:10]) + "]"
    return _scalar(value) or "-"


def _change_line(change: Mapping[str, Any]) -> str:
    """``code path attribute before -> after``, every value sanitised, the line capped."""
    parts = [_pick(change, "code") or _UNKNOWN, _pick(change, "path") or _UNKNOWN]
    attribute = _pick(change, "attribute")
    if attribute:
        parts.append(attribute)
    if "before" in change or "after" in change:
        parts.append(f"{_value_text(change.get('before'))} -> {_value_text(change.get('after'))}")
    return "  " + sanitize_header_text(" ".join(parts), _CHANGE_LINE_LIMIT)


def _breaking_block(payload: Mapping[str, Any]) -> list[str]:
    """One block per pinned version: at most ``MAX_EMAIL_CHANGES`` change lines in all, then
    ``and N more, see <diff URL>`` for what was left out of a version."""
    sections = payload.get("pinned_versions")
    if not isinstance(sections, list):
        return []
    lines: list[str] = []
    used = 0
    rendered = 0
    overflow = 0

    def emit(line: str) -> bool:
        nonlocal used
        if used + len(line) + 1 > _BLOCK_CHAR_BUDGET:
            return False
        lines.append(line)
        used += len(line) + 1
        return True

    for section in sections:
        if not isinstance(section, Mapping):
            continue
        changes = [c for c in section.get("changes") or [] if isinstance(c, Mapping)]
        total = max(_int(section.get("changes_total")), len(changes))
        semver = sanitize_header_text(_pick(section, "semver") or "", _FIELD_SUBJECT_LIMIT)
        count = _int(section.get("breaking_count"))
        header = f"Pinned version {semver or _UNKNOWN}: {count} breaking change(s)"
        if not emit(header):
            overflow += total
            continue
        shown = 0
        for change in changes:
            if rendered >= MAX_EMAIL_CHANGES or not emit(_change_line(change)):
                break
            rendered += 1
            shown += 1
        hidden = total - shown
        if hidden > 0:
            url = _pick(section, "diff_url") or ""
            more = f"and {hidden} more"
            if url.startswith("/api/"):
                more += f", see {url}"
            if not emit("  " + sanitize_header_text(more, _CHANGE_LINE_LIMIT)):
                overflow += hidden
    if overflow:
        lines.append(f"and {overflow} more breaking changes; compare the versions in the API")
    return lines


Templates = Mapping[str, Mapping[str, str]]


def _template(templates: Templates | None, event_type: str, key: str, default: str) -> str:
    stored = (templates or {}).get(
        event_type if event_type in DEFAULT_EMAIL_SUBJECTS else "generic"
    )
    value = stored.get(key) if isinstance(stored, Mapping) else None
    return value if isinstance(value, str) and value else default


def render_email(
    notice: NoticeLike, card_name: str, templates: Templates | None = None
) -> tuple[str, str]:
    """The subject and the plain-text body of one notice, safe to send as ``MailSendIn``.

    ``templates`` maps an event type (or ``generic``) to a stored ``{subject?, body?}`` override;
    without one the defaults are used. A stored template that does not render falls back to the
    default, so a bad row never stops a notice.
    """
    payload = notice.payload
    contract_id = _pick(payload.get("contract"), "id")
    odcs_raw = _pick(payload.get("contract"), "odcs_id")
    semver_raw = _pick(payload.get("version"), "semver")

    odcs = sanitize_header_text(odcs_raw or "", ODCS_ID_SUBJECT_LIMIT) or _UNKNOWN
    semver = sanitize_header_text(semver_raw or "", _FIELD_SUBJECT_LIMIT) or _UNKNOWN
    previous = payload.get("previous_latest")
    fields: dict[str, str] = {
        "odcs": odcs,
        "semver": semver,
        "card": sanitize_header_text(card_name, ODCS_ID_SUBJECT_LIMIT) or _UNKNOWN,
        "event": sanitize_header_text(notice.event_type, ODCS_ID_SUBJECT_LIMIT) or _UNKNOWN,
        "previous_latest": sanitize_header_text(
            _pick(previous, "semver") or "", _FIELD_SUBJECT_LIMIT
        )
        or _UNKNOWN,
    }
    for key, _label in _EVENT_FIELDS:
        raw = _scalar(payload.get(key))
        fields[key] = sanitize_header_text(raw or "", _FIELD_SUBJECT_LIMIT) or _UNKNOWN
    event_key = notice.event_type if notice.event_type in DEFAULT_EMAIL_SUBJECTS else _GENERIC_EVENT
    default_subject = DEFAULT_EMAIL_SUBJECTS[event_key]
    subject_template = _template(templates, notice.event_type, "subject", default_subject)
    try:
        subject = render_template(subject_template, fields, "subject")
    except TemplateError:
        subject = render_template(default_subject, fields, "subject")
    # Re-sanitised after substitution (one line, no controls), cut to the mail service's cap.
    subject = sanitize_header_text(subject, MAX_SUBJECT_CHARS) or sanitize_header_text(
        render_template(default_subject, fields, "subject"), MAX_SUBJECT_CHARS
    )

    def line(label: str, raw: str | None) -> str | None:
        if raw is None:
            return None
        text = sanitize_header_text(raw, _BODY_VALUE_LIMIT)
        return f"{label}: {text}" if text else None

    lines: list[str | None] = [
        line("Card", card_name),
        line("Event", notice.event_type),
        line("Contract id", contract_id),
        line("ODCS id", odcs_raw),
        line("Version", semver_raw),
    ]
    lines.extend(line(label, _scalar(payload.get(key))) for key, label in _EVENT_FIELDS)
    lines.append(line("Previous Latest version", _pick(previous, "semver")))
    lines.append(line("Notice id", str(notice.id)))
    lines.append(line("Occurred at", notice.occurred_at.isoformat()))
    details = "\n".join(entry for entry in lines if entry)
    breaking = ""
    if notice.event_type == _BREAKING:
        block = _breaking_block(payload)
        if block:
            breaking = "\n\nBreaking changes for the versions your card pins:\n" + "\n".join(block)
    body_fields = {
        **fields,
        "subject": subject,
        "details": details,
        "breaking_changes": breaking,
        "contract_id": sanitize_header_text(contract_id or "", _BODY_VALUE_LIMIT) or _UNKNOWN,
        "notice_id": str(notice.id),
        "occurred_at": notice.occurred_at.isoformat(),
    }
    body_template = _template(templates, notice.event_type, "body", DEFAULT_EMAIL_BODY)
    try:
        text = render_template(body_template, body_fields, "body")
    except TemplateError:
        text = render_template(DEFAULT_EMAIL_BODY, body_fields, "body")
    return subject, text[:MAX_TEXT_CHARS]


@dataclass(frozen=True)
class EmailOutcome:
    """The result of one hand-over. ``error`` is a fixed code (module docstring), never a body.

    ``refused`` counts recipients the SMTP server refused; with ``ok`` true it is a partial refusal
    (the accepted recipients are not sent again). ``status`` is the mail service's HTTP status when
    it answered.
    """

    ok: bool
    refused: int
    error: str | None
    status: int | None = None


async def send_email(
    client: httpx.AsyncClient,
    settings: ContractsSettings,
    recipients: Sequence[str],
    subject: str,
    text: str,
    *,
    bearer: str | None = None,
) -> EmailOutcome:
    """POST one message to the mail service and map the answer to an outcome.

    The delivery worker presents the notifier's key; the Settings test email passes the caller's
    own ``bearer`` instead (a Global admin holds ``mail:send``).
    """
    key = (
        bearer
        if bearer is not None
        else (
            settings.mail_api_key.get_secret_value() if settings.mail_api_key is not None else None
        )
    )
    if not settings.mail_url or key is None:
        return EmailOutcome(False, 0, "mail-not-configured")
    try:
        content = MailSendIn(to=list(recipients), subject=subject, text=text).model_dump_json()
    except ValidationError:
        # A recipient or a rendered field the mail service would refuse as well: never sent.
        return EmailOutcome(False, 0, "mail-invalid-message")
    try:
        response = await client.post(
            settings.mail_url.rstrip("/") + MAIL_SEND_PATH,
            content=content,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            timeout=settings.mail_timeout_s,
        )
    except httpx.TimeoutException:
        return EmailOutcome(False, 0, "mail-timeout")
    except httpx.HTTPError:
        return EmailOutcome(False, 0, "mail-connect-error")
    if response.status_code != 200:
        return EmailOutcome(
            False, 0, f"mail-http-{response.status_code}", status=response.status_code
        )
    try:
        answer = MailSendOut.model_validate_json(response.content)
    except ValidationError:
        return EmailOutcome(False, 0, "mail-bad-response", status=200)
    if answer.deferred > 0:
        # A temporary refusal (greylisting, mailbox full): those recipients never got the notice, so
        # the delivery is a failed attempt and is retried. The accepted recipients may get a second
        # copy; a duplicate is better than a lost deprecation warning.
        return EmailOutcome(False, answer.refused, "mail-recipients-deferred", status=200)
    if answer.accepted < 1:
        error = "mail-recipients-refused" if answer.refused > 0 else "mail-bad-response"
        return EmailOutcome(False, answer.refused, error, status=200)
    return EmailOutcome(True, max(answer.refused, 0), None, status=200)


__all__ = [
    "EMAIL_SUBJECTS",
    "MAIL_SEND_PATH",
    "EmailOutcome",
    "Templates",
    "render_email",
    "sanitize_header_text",
    "send_email",
]
