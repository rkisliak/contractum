"""Platform settings: the section models, the export format and the cached runtime client.

Quick 261120-0yc (decisions D-01 to D-17). Identity owns one table of *stored overrides*, one row
per section. A field that is absent means "use the owning service's Helm/env value", so Helm and
environment values stay the defaults. This module is the one place that knows the sections:

* ``SECTIONS`` maps a section name to its Pydantic model (``extra="forbid"``, every field
  optional, bounds in the model). Later plans register more sections here and change nothing else.
* ``RUNTIME_SECTIONS`` are the sections other services may read through identity's internal
  ``GET /internal/v1/settings/runtime``.
* ``EXPORT_FORMAT`` / ``parse_export`` are the versioned, section-keyed export file and its
  validation (a typed error carries a JSON pointer).
* ``RuntimeSettingsClient`` is how another service reads a runtime section: 30 s cache, keeps
  the last good value when identity cannot answer, ``None`` only when it never read the section.

Nothing secret can be stored: secrets stay Docker/Kubernetes secrets. ``dc_core`` imports no other
``dc_*`` package, so checks that need identity data (the tenant list, the folder tree) stay in
identity.
"""

from __future__ import annotations

import ipaddress
import re
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import httpx
import structlog
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from pydantic import model_validator as _model_validator
from pydantic_core import PydanticCustomError

from dc_core.http import make_client
from dc_core.names import name_problem

_log = structlog.get_logger("dc_core.platform_settings")

EXPORT_FORMAT = "contractum-settings"
EXPORT_FORMAT_VERSION = 1
BANNER_TEXT_MAX = 500
_GUID_SHAPE = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
# Bidi embedding, override and isolate characters (U+202A to U+202E, U+2066 to U+2069).
_BIDI_CONTROLS = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))


class SettingsModel(BaseModel):
    """Base of every section model: unknown fields are refused, every field is optional."""

    model_config = ConfigDict(extra="forbid")


def check_plain_text(value: str) -> str:
    """Plain display text: no control character (newline included), NUL, lone surrogate or bidi
    embedding, override or isolate character. Returns the trimmed value; raises ``ValueError``
    with a fixed message (the input is never echoed)."""
    for ch in value:
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF:
            raise ValueError("text must not contain a lone surrogate")
        if ch in _BIDI_CONTROLS:
            raise ValueError("text must not contain a bidirectional control character")
        if unicodedata.category(ch) == "Cc":  # C0, C1, NUL and newline
            raise ValueError("text must not contain a control character")
    return value.strip()


class EntraSettings(SettingsModel):
    """E1 and E5: the Entra default tenant and the default owner of a registration."""

    default_tenant_id: str | None = Field(default=None, pattern=_GUID_SHAPE)
    default_owner_folder_id: uuid.UUID | None = None


class Banner(SettingsModel):
    """P1: the announcement banner (plain text, shown while active)."""

    text: str = Field(min_length=1, max_length=BANNER_TEXT_MAX)
    severity: Literal["info", "warning"] = "info"
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None

    @field_validator("text")
    @classmethod
    def _plain_text(cls, value: str) -> str:
        text = check_plain_text(value)
        if not text:
            raise ValueError("text must not be blank")
        return text

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @_model_validator(mode="after")
    def _ordered(self) -> Banner:
        if (
            self.starts_at is not None
            and self.ends_at is not None
            and self.ends_at <= self.starts_at
        ):
            raise ValueError("ends_at must be after starts_at")
        return self

    def active_at(self, now: datetime) -> bool:
        if self.starts_at is not None and now < self.starts_at:
            return False
        return not (self.ends_at is not None and now >= self.ends_at)


AUDIT_RETENTION_MIN_DAYS, AUDIT_RETENTION_MAX_DAYS = 90, 36500


class PlatformSettings(SettingsModel):
    """P1 and P2: platform-wide display settings and the audit retention (0ye, D-33).

    ``audit_retention_days`` is ``0`` (keep every event for ever) or 90 to 36500; absent means the
    audit service's own ``DC_AUDIT_RETENTION_DAYS`` (unset: keep for ever).
    """

    banner: Banner | None = None
    audit_retention_days: int | None = Field(default=None, ge=0, le=AUDIT_RETENTION_MAX_DAYS)

    @field_validator("audit_retention_days")
    @classmethod
    def _retention(cls, value: int | None) -> int | None:
        if value is not None and 0 < value < AUDIT_RETENTION_MIN_DAYS:
            raise ValueError(f"Use 0 (keep for ever) or at least {AUDIT_RETENTION_MIN_DAYS} days.")
        return value


# --- notifications (quick 261120-0yd, D-18 to D-26) ---------------------------------------------

SUBJECT_PREFIX = "[Contractum]"
# The event types an email template can be set for: today's seven notice types plus the fallback.
EVENT_TYPES: tuple[str, ...] = (
    "contract.version.published",
    "contract.version.breaking_published",
    "contract.latest.changed",
    "contract.version.deprecated",
    "contract.version.sunset_approaching",
    "contract.version.unpublish_blocked",
    "card.subscription.removed",
    "generic",
)
# One subject template per event type (D-20): byte for byte what the mailer produced before
# templates became editable. ``{{`` and ``}}`` are the literal braces of the template syntax.
DEFAULT_EMAIL_SUBJECTS: Mapping[str, str] = {
    "contract.version.published": f"{SUBJECT_PREFIX} {{odcs}} {{semver}} published",
    "contract.version.breaking_published": (
        f"{SUBJECT_PREFIX} {{odcs}} {{semver}} published with breaking changes"
    ),
    "contract.latest.changed": f"{SUBJECT_PREFIX} {{odcs}} {{semver}} is now the Latest version",
    "contract.version.deprecated": (
        f"{SUBJECT_PREFIX} {{odcs}} {{semver}} deprecated (sunset {{sunset_date}})"
    ),
    "contract.version.sunset_approaching": (
        f"{SUBJECT_PREFIX} {{odcs}} {{semver}} sunset approaching ({{sunset_date}})"
    ),
    "contract.version.unpublish_blocked": (
        f"{SUBJECT_PREFIX} {{odcs}} {{semver}}: the owner wants to unpublish it"
    ),
    "card.subscription.removed": f"{SUBJECT_PREFIX} your pin of {{odcs}} {{semver}} was removed",
    "generic": f"{SUBJECT_PREFIX} notice for {{odcs}}",
}
DEFAULT_EMAIL_BODY = (
    "{subject}\n\n{details}{breaking_changes}\n\n"
    "You get this message because your subscriber card has the email channel on.\n"
)
SUBJECT_PLACEHOLDERS: tuple[str, ...] = (
    "odcs",
    "semver",
    "card",
    "event",
    "status",
    "sunset_date",
    "lead_days",
    "deprecated_at",
    "reason",
    "previous_latest",
)
BODY_PLACEHOLDERS: tuple[str, ...] = (
    *SUBJECT_PLACEHOLDERS,
    "subject",
    "details",
    "breaking_changes",
    "contract_id",
    "notice_id",
    "occurred_at",
)
SUBJECT_MIN, SUBJECT_MAX = 1, 200
BODY_MIN, BODY_MAX = 1, 8000
RETRY_SCHEDULE_MAX_ENTRIES = 10
RETRY_DELAY_MIN_S, RETRY_DELAY_MAX_S = 10, 86400
MAX_ATTEMPTS_MAX = 20
SUNSET_LEADS_MAX_ENTRIES = 5
SUNSET_LEAD_MAX_DAYS = 365
ALLOWED_HOSTS_MAX = 100
HOST_MAX_CHARS = 253
SENDER_NAME_MAX = 100
SENDER_ADDRESS_MAX = 254
# The platform's built-in notification defaults (the same values `ContractsSettings` ships).
BUILTIN_RETRY_SCHEDULE_S: tuple[int, ...] = (60, 300, 1800, 7200, 21600, 43200)
BUILTIN_MAX_ATTEMPTS = 7
BUILTIN_SUNSET_LEAD_DAYS: tuple[int, ...] = (7, 30)

_PLACEHOLDER_NAME = re.compile(r"[a-z_]{1,40}")


class TemplateError(ValueError):
    """A template that is not acceptable. The message is fixed text; at most a placeholder name
    of a harmless shape is named, never any other part of the input."""


def _tokens(text: str, allowed: frozenset[str]) -> Iterator[tuple[str, str]]:
    """The one tokenizer of validation and rendering: ``("text", s)`` and ``("field", name)``.

    ``{{`` and ``}}`` are literal braces; ``{name}`` only for an allow-listed name. Everything
    else with a brace (attribute or index access, conversions, format specs, a lone brace) is
    refused: there is no expression language here, only substitution.
    """
    i, n = 0, len(text)
    literal: list[str] = []
    while i < n:
        ch = text[i]
        if ch == "{":
            if text.startswith("{{", i):
                literal.append("{")
                i += 2
                continue
            end = text.find("}", i + 1)
            name = text[i + 1 : end] if end != -1 else ""
            if end == -1 or "{" in name:
                raise TemplateError("A '{' must be doubled ('{{') or start a {placeholder}.")
            if name not in allowed:
                shown = f" '{name}'" if _PLACEHOLDER_NAME.fullmatch(name) else ""
                raise TemplateError(f"Unknown placeholder{shown}.")
            if literal:
                yield "text", "".join(literal)
                literal = []
            yield "field", name
            i = end + 1
        elif ch == "}":
            if text.startswith("}}", i):
                literal.append("}")
                i += 2
                continue
            raise TemplateError("A '}' must be doubled ('}}').")
        else:
            literal.append(ch)
            i += 1
    if literal:
        yield "text", "".join(literal)


def _allowed(kind: str) -> frozenset[str]:
    if kind == "subject":
        return frozenset(SUBJECT_PLACEHOLDERS)
    if kind == "body":
        return frozenset(BODY_PLACEHOLDERS)
    raise ValueError("kind must be 'subject' or 'body'")


def validate_template(text: str, kind: Literal["subject", "body"]) -> str:
    """The normalised template (a body's ``\\r\\n`` becomes ``\\n``), or ``TemplateError``.

    A subject has 1 to 200 characters and no control character; a body 1 to 8000 characters and
    no control character other than a newline. Placeholders come from the allow-list of ``kind``.
    """
    if kind == "body":
        text = text.replace("\r\n", "\n")
        low, high = BODY_MIN, BODY_MAX
    else:
        low, high = SUBJECT_MIN, SUBJECT_MAX
    allowed = _allowed(kind)
    if not low <= len(text) <= high:
        raise TemplateError(f"The {kind} must have {low} to {high} characters.")
    for ch in text:
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF:
            raise TemplateError("The text must not contain a lone surrogate.")
        if ch in _BIDI_CONTROLS:
            raise TemplateError("The text must not contain a bidirectional control character.")
        if unicodedata.category(ch) == "Cc" and not (kind == "body" and ch == "\n"):
            raise TemplateError("The text must not contain a control character.")
    for _ in _tokens(text, allowed):  # raises on the first bad brace or name
        pass
    if kind == "subject" and not text.strip():
        raise TemplateError("The subject must not be blank.")
    return text


def render_template(text: str, fields: Mapping[str, str], kind: Literal["subject", "body"]) -> str:
    """Plain substitution of ``{name}`` by ``fields[name]`` (a missing name renders as empty).

    The values are used as they are: the caller has already sanitised and bounded them. Nothing
    is evaluated: no ``str.format``, no attribute or index access.
    """
    parts: list[str] = []
    for is_field, value in _tokens(text, _allowed(kind)):
        parts.append(fields.get(value, "") if is_field == "field" else value)
    return "".join(parts)


_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_RE = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")
_ADDRESS_FORBIDDEN = frozenset('<>()[],;:"\\')
_LOCAL_PART_RE = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}")
_DOMAIN_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)


def check_host_pattern(value: str) -> str:
    """An allowed-hosts entry: a lowercase ASCII hostname, or ``*.`` plus one. No IP literal, port,
    scheme or path. Returns the value; raises ``ValueError`` with a fixed message."""
    host = value[2:] if value.startswith("*.") else value
    if (
        not host
        or len(host) > HOST_MAX_CHARS
        or _HOST_RE.fullmatch(host) is None
        or host.rpartition(".")[2].isdigit()  # an IPv4 literal (or any all-digit TLD)
    ):
        raise ValueError(
            "Enter a lowercase hostname such as hooks.example.com or *.example.com "
            "(no address, port or scheme)."
        )
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return value
    raise ValueError("An IP address is not a hostname.")  # pragma: no cover - guarded above


def webhook_host_allowed(host: str, patterns: Sequence[str]) -> bool:
    """Does ``host`` match an allowed-hosts entry? The list is mandatory: an empty one allows no
    host (``False``; quick 261120-s0p, item 6).

    Exact match, or for ``*.example.com`` any host below it (not ``example.com`` itself),
    case-insensitive after IDNA. This only ever narrows: the caller still applies every other
    webhook check (https, no userinfo, globally routable address).
    """
    if not patterns:
        return False
    try:
        name = host.strip().rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return False
    for pattern in patterns:
        if pattern.startswith("*."):
            if name.endswith(pattern[1:]) and len(name) > len(pattern) - 1:
                return True
        elif name == pattern:
            return True
    return False


def check_email_address(value: str) -> str:
    """A plain address ``local@domain`` (D-26): at most 254 characters, exactly one ``@``, ASCII,
    no whitespace, control or ``<>()[],;:"\\`` character, LDH domain labels. Fixed error text."""
    message = "Enter a plain email address such as name@example.com."
    if len(value) > SENDER_ADDRESS_MAX or value.count("@") != 1 or not value.isascii():
        raise ValueError(message)
    if any(
        ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F or ch in _ADDRESS_FORBIDDEN
        for ch in value
    ):
        raise ValueError(message)
    local, _, domain = value.partition("@")
    if (
        _LOCAL_PART_RE.fullmatch(local) is None
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or _DOMAIN_RE.fullmatch(domain) is None
    ):
        raise ValueError(message)
    return value


class EmailTemplate(SettingsModel):
    """A stored override of one event type's subject and/or body."""

    subject: str | None = None
    body: str | None = None

    @field_validator("subject")
    @classmethod
    def _subject(cls, value: str | None) -> str | None:
        return None if value is None else _template_value(value, "subject")

    @field_validator("body")
    @classmethod
    def _body(cls, value: str | None) -> str | None:
        return None if value is None else _template_value(value, "body")


def _template_value(value: str, kind: Literal["subject", "body"]) -> str:
    try:
        return validate_template(value, kind)
    except TemplateError as exc:
        raise ValueError(str(exc)) from None


class Sender(SettingsModel):
    """N2: the From name and address of notification emails."""

    name: str | None = Field(default=None, max_length=SENDER_NAME_MAX)
    address: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # The one name rule (quick 261120-lbz, D-01); the bare detail is the message.
        detail, trimmed = name_problem(value)
        if detail is not None:
            raise PydanticCustomError("name_invalid", detail)
        return trimmed

    @field_validator("address")
    @classmethod
    def _address(cls, value: str | None) -> str | None:
        return None if value is None else check_email_address(value)


class NotificationsSettings(SettingsModel):
    """N1 to N6. Every field is an override of the Helm/env value of the service that owns it."""

    email_templates: dict[str, EmailTemplate] | None = None
    sender: Sender | None = None
    retry_schedule_s: (
        list[Annotated[int, Field(ge=RETRY_DELAY_MIN_S, le=RETRY_DELAY_MAX_S)]] | None
    ) = Field(default=None, min_length=1, max_length=RETRY_SCHEDULE_MAX_ENTRIES)
    max_attempts: int | None = Field(default=None, ge=1, le=MAX_ATTEMPTS_MAX)
    sunset_lead_days: list[Annotated[int, Field(ge=1, le=SUNSET_LEAD_MAX_DAYS)]] | None = Field(
        default=None, min_length=1, max_length=SUNSET_LEADS_MAX_ENTRIES
    )
    webhook_allowed_hosts: list[str] | None = Field(default=None, max_length=ALLOWED_HOSTS_MAX)

    @field_validator("email_templates")
    @classmethod
    def _known_types(
        cls, value: dict[str, EmailTemplate] | None
    ) -> dict[str, EmailTemplate] | None:
        if value is None:
            return None
        if any(key not in EVENT_TYPES for key in value):
            raise ValueError("Unknown event type.")
        kept = {k: v for k, v in value.items() if v.subject is not None or v.body is not None}
        return kept or None

    @field_validator("sunset_lead_days")
    @classmethod
    def _leads(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if len(set(value)) != len(value):
            raise ValueError("Each lead time may appear once.")
        return sorted(value)

    @field_validator("webhook_allowed_hosts")
    @classmethod
    def _hosts(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for entry in value:
            check_host_pattern(entry)
        unique = list(dict.fromkeys(value))
        return unique or None


def notifications_defaults(
    *,
    retry_schedule_s: Sequence[int] = BUILTIN_RETRY_SCHEDULE_S,
    max_attempts: int = BUILTIN_MAX_ATTEMPTS,
    sunset_lead_days: Sequence[int] = BUILTIN_SUNSET_LEAD_DAYS,
) -> dict[str, Any]:
    """The contracts-owned half of the notifications defaults (what contracts' about reports)."""
    return {
        "email_templates": {
            event: {"subject": DEFAULT_EMAIL_SUBJECTS[event], "body": DEFAULT_EMAIL_BODY}
            for event in EVENT_TYPES
        },
        "retry_schedule_s": list(retry_schedule_s),
        "max_attempts": max_attempts,
        "sunset_lead_days": list(sunset_lead_days),
        "webhook_allowed_hosts": [],
    }


# --- governance and security (quick 261120-0ye, D-31, D-32) ------------------------------------

ODCS_VERSION_PATTERN = r"^v\d+\.\d+\.\d+$"
MANDATORY_WARN_MAX_DAYS = 365
CREDENTIAL_TTL_MAX_DAYS = 3650
# Quick 261120-lbz, D-03: active access tokens one owner (a user, a service account) may hold.
ACTIVE_TOKENS_MIN, ACTIVE_TOKENS_MAX = 1, 1000
# Quick 261120-s0p, D-09: concurrent sessions one local user may hold; signing in beyond it ends
# the oldest session.
SESSIONS_PER_USER_MIN, SESSIONS_PER_USER_MAX = 1, 100
SESSION_MAX_MIN_S, SESSION_MAX_MAX_S = 3600, 7_776_000
IDLE_TIMEOUT_MIN_S, IDLE_TIMEOUT_MAX_S = 900, 604_800
ACCOUNT_THRESHOLD_MIN, ACCOUNT_THRESHOLD_MAX = 3, 20
IP_THRESHOLD_MIN, IP_THRESHOLD_MAX = 10, 500
MAX_LOCK_MIN_S, MAX_LOCK_MAX_S = 60, 86_400
PASSWORD_MIN_LENGTH_FLOOR, PASSWORD_MIN_LENGTH_CEILING = 12, 128


class GovernanceSettings(SettingsModel):
    """G1 to G3: the rule-set grace period, the default ODCS version and the lifecycle reason."""

    mandatory_warn_days: int | None = Field(default=None, ge=0, le=MANDATORY_WARN_MAX_DAYS)
    default_odcs_api_version: str | None = Field(default=None, pattern=ODCS_VERSION_PATTERN)
    require_lifecycle_reason: StrictBool | None = None


class SecuritySettings(SettingsModel):
    """S1 to S5: sign-in methods, credential ceiling, session limits, lockout and password rules.

    Identity only: this section is never served by the runtime route. Every bound is a floor or
    ceiling that keeps the control meaningful (a minimum password length never below 12).
    """

    local_signin_enabled: StrictBool | None = None
    microsoft_signin_enabled: StrictBool | None = None
    credential_max_ttl_days: int | None = Field(default=None, ge=1, le=CREDENTIAL_TTL_MAX_DAYS)
    max_active_tokens: int | None = Field(
        default=None, ge=ACTIVE_TOKENS_MIN, le=ACTIVE_TOKENS_MAX, strict=True
    )
    max_sessions_per_user: int | None = Field(
        default=None, ge=SESSIONS_PER_USER_MIN, le=SESSIONS_PER_USER_MAX, strict=True
    )
    session_max_s: int | None = Field(default=None, ge=SESSION_MAX_MIN_S, le=SESSION_MAX_MAX_S)
    idle_timeout_s: int | None = Field(default=None, ge=IDLE_TIMEOUT_MIN_S, le=IDLE_TIMEOUT_MAX_S)
    lockout_account_threshold: int | None = Field(
        default=None, ge=ACCOUNT_THRESHOLD_MIN, le=ACCOUNT_THRESHOLD_MAX
    )
    lockout_ip_threshold: int | None = Field(default=None, ge=IP_THRESHOLD_MIN, le=IP_THRESHOLD_MAX)
    lockout_max_lock_s: int | None = Field(default=None, ge=MAX_LOCK_MIN_S, le=MAX_LOCK_MAX_S)
    password_min_length: int | None = Field(
        default=None, ge=PASSWORD_MIN_LENGTH_FLOOR, le=PASSWORD_MIN_LENGTH_CEILING
    )
    password_require_upper: StrictBool | None = None
    password_require_lower: StrictBool | None = None
    password_require_digit: StrictBool | None = None
    password_require_symbol: StrictBool | None = None

    @field_validator("idle_timeout_s")
    @classmethod
    def _idle_within_session(cls, value: int | None, info: ValidationInfo) -> int | None:
        session_max = info.data.get("session_max_s")
        if value is not None and session_max is not None and value > session_max:
            raise ValueError("The idle timeout must not exceed the session length.")
        return value  # (the session length is validated first: it is declared first)


SECTIONS: Mapping[str, type[SettingsModel]] = {
    "entra": EntraSettings,
    "notifications": NotificationsSettings,
    "governance": GovernanceSettings,
    "security": SecuritySettings,
    "platform": PlatformSettings,
}
# The sections other services may read from identity. ``security`` is identity's own.
RUNTIME_SECTIONS: frozenset[str] = frozenset({"platform", "notifications", "governance"})
# The fixed tab order of the UI (D-16); a tab shows only when its section exists.
SECTION_ORDER: tuple[str, ...] = ("entra", "notifications", "governance", "security", "platform")


def normalise_section(section: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate ``raw`` against the section model and return the stored overrides (JSON types).

    ``None`` values and defaults-by-absence are dropped, so the same values always store as the
    same document (a no-op save is detectable). Raises ``pydantic.ValidationError``.
    """
    model = SECTIONS[section].model_validate(dict(raw))
    return model.model_dump(mode="json", exclude_none=True)


class SettingsImportError(Exception):
    """An export file that is not valid: ``pointer`` is a JSON pointer into the file."""

    def __init__(self, pointer: str, message: str) -> None:
        super().__init__(message)
        self.pointer = pointer
        self.message = message


def _pointer(loc: tuple[Any, ...], base: str) -> str:
    parts = [str(p).replace("~", "~0").replace("/", "~1") for p in loc]
    return base + "".join(f"/{p}" for p in parts)


def build_export(
    sections: Mapping[str, Mapping[str, Any]], *, now: datetime | None = None
) -> dict[str, Any]:
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_FORMAT_VERSION,
        "exported_at": stamp,
        "sections": {name: dict(sections[name]) for name in sorted(sections)},
    }


def parse_export(data: object) -> dict[str, dict[str, Any]]:
    """Validate an export document; returns ``{section: normalised stored overrides}``.

    A section missing from the file is simply absent from the result (it stays unchanged on
    import). Raises ``SettingsImportError`` carrying a JSON pointer for an unknown format,
    version, section or field.
    """
    if not isinstance(data, dict):
        raise SettingsImportError("", "the file must be a JSON object")
    for key in data:
        if key not in ("format", "format_version", "exported_at", "sections"):
            raise SettingsImportError(_pointer((key,), ""), "unknown top-level field")
    if data.get("format") != EXPORT_FORMAT:
        raise SettingsImportError("/format", "not a Contractum settings file")
    version = data.get("format_version")
    if isinstance(version, bool) or version != EXPORT_FORMAT_VERSION:
        raise SettingsImportError("/format_version", "unsupported format version")
    exported_at = data.get("exported_at")
    if exported_at is not None and not isinstance(exported_at, str):
        raise SettingsImportError("/exported_at", "exported_at must be a string")
    sections = data.get("sections")
    if not isinstance(sections, dict):
        raise SettingsImportError("/sections", "sections must be an object")
    result: dict[str, dict[str, Any]] = {}
    for name, raw in sections.items():
        if name not in SECTIONS:
            raise SettingsImportError(_pointer((name,), "/sections"), "unknown settings section")
        if not isinstance(raw, dict):
            raise SettingsImportError(_pointer((name,), "/sections"), "a section must be an object")
        try:
            result[name] = normalise_section(name, raw)
        except ValidationError as exc:
            first = exc.errors()[0]
            raise SettingsImportError(
                _pointer(tuple(first["loc"]), _pointer((name,), "/sections")),
                "invalid or unknown setting",
            ) from None
    return result


class RuntimeSettingsClient:
    """Reads a runtime section from identity with a short cache and a last-good fallback (D-06).

    ``get`` never raises. A read failure logs ``settings_unavailable`` (error class only), keeps
    serving the last good value and is retried no sooner than ``fallback_ttl_s``. ``None`` means
    the section was never read: the caller uses its Helm/env default.
    """

    def __init__(
        self,
        base_url: str,
        *,
        ttl_s: float = 30.0,
        fallback_ttl_s: float = 5.0,
        timeout_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._ttl = ttl_s
        self._fallback_ttl = fallback_ttl_s
        self._timeout = timeout_s
        self._clock = clock
        self._http = client
        self._good: dict[str, dict[str, Any]] = {}
        self._next_try: dict[str, float] = {}

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = make_client(timeout_s=self._timeout)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    def invalidate(self) -> None:
        """Forget the retry deadlines: the next ``get`` reads again (the last good value stays)."""
        self._next_try.clear()

    async def get(self, section: str, *, bearer: str) -> dict[str, Any] | None:
        now = self._clock()
        if now < self._next_try.get(section, 0.0):
            return self._good.get(section)
        try:
            response = await self._client().get(
                f"{self._base}/internal/v1/settings/runtime",
                params={"sections": section},
                headers={"Authorization": f"Bearer {bearer}"},
                timeout=self._timeout,
            )
            if response.status_code != 200:
                raise _UnexpectedStatus
            body = response.json()
            value = body["sections"][section]
            if not isinstance(value, dict):
                raise _UnexpectedStatus
        except (httpx.HTTPError, ValueError, KeyError, TypeError, _UnexpectedStatus) as exc:
            _log.warning("settings_unavailable", section=section, error=type(exc).__name__)
            self._next_try[section] = now + self._fallback_ttl
            return self._good.get(section)
        self._good[section] = value
        self._next_try[section] = now + self._ttl
        return value


class _UnexpectedStatus(Exception):
    """Identity answered, but not with a usable runtime section."""


__all__ = [
    "ALLOWED_HOSTS_MAX",
    "BANNER_TEXT_MAX",
    "BODY_PLACEHOLDERS",
    "BUILTIN_MAX_ATTEMPTS",
    "BUILTIN_RETRY_SCHEDULE_S",
    "BUILTIN_SUNSET_LEAD_DAYS",
    "DEFAULT_EMAIL_BODY",
    "DEFAULT_EMAIL_SUBJECTS",
    "EVENT_TYPES",
    "EXPORT_FORMAT",
    "EXPORT_FORMAT_VERSION",
    "RUNTIME_SECTIONS",
    "SECTIONS",
    "SECTION_ORDER",
    "SUBJECT_PLACEHOLDERS",
    "SUBJECT_PREFIX",
    "Banner",
    "EmailTemplate",
    "EntraSettings",
    "GovernanceSettings",
    "NotificationsSettings",
    "PlatformSettings",
    "RuntimeSettingsClient",
    "Sender",
    "SettingsImportError",
    "SecuritySettings",
    "SettingsModel",
    "TemplateError",
    "build_export",
    "check_email_address",
    "check_host_pattern",
    "check_plain_text",
    "normalise_section",
    "notifications_defaults",
    "parse_export",
    "render_template",
    "validate_template",
    "webhook_host_allowed",
]
