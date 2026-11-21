"""Platform settings models, export format and runtime client (quick 261120-0yc)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from dc_core.platform_settings import (
    BODY_PLACEHOLDERS,
    DEFAULT_EMAIL_BODY,
    DEFAULT_EMAIL_SUBJECTS,
    EVENT_TYPES,
    EXPORT_FORMAT,
    RUNTIME_SECTIONS,
    SECTIONS,
    SUBJECT_PLACEHOLDERS,
    Banner,
    EntraSettings,
    GovernanceSettings,
    NotificationsSettings,
    PlatformSettings,
    RuntimeSettingsClient,
    SecuritySettings,
    SettingsImportError,
    TemplateError,
    build_export,
    check_email_address,
    check_host_pattern,
    check_plain_text,
    normalise_section,
    notifications_defaults,
    parse_export,
    render_template,
    validate_template,
    webhook_host_allowed,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit

GUID = "11111111-2222-4333-8444-555555555555"
BASE = "http://identity:8000"
URL = f"{BASE}/internal/v1/settings/runtime"


def test_registries_hold_the_foundation_sections() -> None:
    assert set(SECTIONS) == {"entra", "notifications", "governance", "security", "platform"}
    assert RUNTIME_SECTIONS == {"platform", "notifications", "governance"}
    assert "security" not in RUNTIME_SECTIONS
    assert RUNTIME_SECTIONS <= set(SECTIONS)


def test_every_field_is_optional_and_extras_are_forbidden() -> None:
    assert EntraSettings().model_dump() == {
        "default_tenant_id": None,
        "default_owner_folder_id": None,
    }
    assert PlatformSettings().banner is None
    with pytest.raises(ValidationError):
        EntraSettings.model_validate({"surprise": 1})
    with pytest.raises(ValidationError):
        PlatformSettings.model_validate({"banner": {"text": "x", "extra": 1}})


@pytest.mark.parametrize("tenant", ["", "not-a-guid", GUID.replace("4", "A"), GUID + "0"])
def test_a_tenant_must_be_a_lowercase_guid(tenant: str) -> None:
    with pytest.raises(ValidationError):
        EntraSettings.model_validate({"default_tenant_id": tenant})


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "a" * 501,
        "line\nbreak",
        "tab\there",
        "nul\x00",
        "c1\x85",
        "\u202eevil",
        "\u2066x",
        "\ud800",
    ],
)
def test_banner_text_rules(text: str) -> None:
    with pytest.raises(ValidationError):
        Banner.model_validate({"text": text})


def test_banner_text_is_trimmed_and_may_be_500_characters() -> None:
    assert Banner.model_validate({"text": "  Maintenance tonight  "}).text == "Maintenance tonight"
    assert len(Banner.model_validate({"text": "x" * 500}).text) == 500


def test_banner_severity_and_window() -> None:
    with pytest.raises(ValidationError):
        Banner.model_validate({"text": "x", "severity": "error"})
    with pytest.raises(ValidationError):
        Banner.model_validate(
            {"text": "x", "starts_at": "2026-01-02T00:00:00Z", "ends_at": "2026-01-01T00:00:00Z"}
        )
    with pytest.raises(ValidationError):
        Banner.model_validate({"text": "x", "starts_at": "2026-01-02T00:00:00"})  # naive
    banner = Banner.model_validate(
        {"text": "x", "starts_at": "2026-01-01T02:00:00+02:00", "ends_at": "2026-01-02T00:00:00Z"}
    )
    assert banner.starts_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert banner.active_at(datetime(2026, 1, 1, 12, tzinfo=UTC))
    assert not banner.active_at(datetime(2025, 12, 31, tzinfo=UTC))
    assert not banner.active_at(datetime(2026, 1, 2, tzinfo=UTC))


def test_normalise_drops_nulls_and_is_stable() -> None:
    a = normalise_section("platform", {"banner": {"text": "Hi", "starts_at": None}})
    assert a == {"banner": {"text": "Hi", "severity": "info"}}
    assert normalise_section("platform", a) == a
    assert normalise_section("entra", {"default_tenant_id": None}) == {}


def test_export_round_trip() -> None:
    sections = {
        "entra": {"default_tenant_id": GUID},
        "platform": {"banner": {"text": "Hi", "severity": "warning"}},
    }
    doc = build_export(sections, now=datetime(2026, 11, 20, 10, 0, tzinfo=UTC))
    assert doc["format"] == EXPORT_FORMAT
    assert doc["format_version"] == 1
    assert doc["exported_at"] == "2026-11-20T10:00:00Z"
    assert parse_export(json.loads(json.dumps(doc))) == sections


def test_a_section_missing_from_the_file_is_absent_from_the_result() -> None:
    assert parse_export({"format": EXPORT_FORMAT, "format_version": 1, "sections": {}}) == {}


def _doc(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {"format": EXPORT_FORMAT, "format_version": 1, "sections": {}}
    doc.update(overrides)
    return doc


@pytest.mark.parametrize(
    ("data", "pointer"),
    [
        ([], ""),
        (_doc(format="other"), "/format"),
        (_doc(format_version=2), "/format_version"),
        (_doc(format_version=True), "/format_version"),
        (_doc(sections=[]), "/sections"),
        (_doc(extra=1), "/extra"),
        (_doc(sections={"nope": {}}), "/sections/nope"),
        (_doc(sections={"entra": []}), "/sections/entra"),
        (_doc(sections={"entra": {"surprise": 1}}), "/sections/entra/surprise"),
        (_doc(sections={"entra": {"default_tenant_id": "x"}}), "/sections/entra/default_tenant_id"),
        (_doc(sections={"platform": {"banner": {"text": ""}}}), "/sections/platform/banner/text"),
    ],
)
def test_import_refusals_carry_a_json_pointer(data: object, pointer: str) -> None:
    with pytest.raises(SettingsImportError) as caught:
        parse_export(data)
    assert caught.value.pointer == pointer


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _client(clock: Clock) -> RuntimeSettingsClient:
    return RuntimeSettingsClient(BASE, clock=clock)


@respx.mock
async def test_client_caches_a_section_for_the_ttl() -> None:
    route = respx.get(URL).respond(200, json={"sections": {"platform": {"banner": {"text": "A"}}}})
    clock = Clock()
    client = _client(clock)
    assert await client.get("platform", bearer="t") == {"banner": {"text": "A"}}
    clock.now += 29
    await client.get("platform", bearer="t")
    assert route.call_count == 1
    clock.now += 2
    await client.get("platform", bearer="t")
    assert route.call_count == 2
    assert route.calls.last.request.headers["authorization"] == "Bearer t"
    assert route.calls.last.request.url.params["sections"] == "platform"
    await client.aclose()


@respx.mock
async def test_client_keeps_the_last_good_value_and_retries_after_the_fallback_ttl() -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(200, json={"sections": {"platform": {"v": 1}}}),
            httpx.Response(503),
            httpx.ConnectError("down"),
            httpx.Response(200, json={"sections": {"platform": {"v": 2}}}),
        ]
    )
    clock = Clock()
    client = _client(clock)
    assert await client.get("platform", bearer="t") == {"v": 1}
    clock.now += 31
    assert await client.get("platform", bearer="t") == {"v": 1}  # 503: last good
    clock.now += 2
    assert await client.get("platform", bearer="t") == {"v": 1}  # inside the fallback ttl
    assert route.call_count == 2
    clock.now += 4
    assert await client.get("platform", bearer="t") == {"v": 1}  # transport error: last good
    clock.now += 6
    assert await client.get("platform", bearer="t") == {"v": 2}
    await client.aclose()


@respx.mock
async def test_client_returns_none_when_a_section_was_never_read() -> None:
    route = respx.get(URL).respond(500)
    clock = Clock()
    client = _client(clock)
    assert await client.get("platform", bearer="t") is None
    assert await client.get("platform", bearer="t") is None
    assert route.call_count == 1  # not retried inside the fallback ttl
    await client.aclose()


@respx.mock
async def test_client_treats_a_malformed_answer_as_unavailable() -> None:
    respx.get(URL).respond(200, json={"sections": {}})
    client = _client(Clock())
    assert await client.get("platform", bearer="t") is None
    await client.aclose()


# --- notifications (quick 261120-0yd) ------------------------------------------------------------


def test_the_default_templates_are_valid_and_cover_every_event_type() -> None:
    assert set(DEFAULT_EMAIL_SUBJECTS) == set(EVENT_TYPES)
    for subject in DEFAULT_EMAIL_SUBJECTS.values():
        assert validate_template(subject, "subject") == subject
    assert validate_template(DEFAULT_EMAIL_BODY, "body") == DEFAULT_EMAIL_BODY


def test_the_subject_allow_list_is_a_subset_of_the_body_allow_list() -> None:
    assert set(SUBJECT_PLACEHOLDERS) < set(BODY_PLACEHOLDERS)
    assert {"subject", "details", "breaking_changes"} <= set(BODY_PLACEHOLDERS)
    assert not {"subject", "details", "contract_id"} & set(SUBJECT_PLACEHOLDERS)


@pytest.mark.parametrize(
    "template",
    [
        "{odcs.__class__}",
        "{odcs[0]}",
        "{0}",
        "{}",
        "{odcs!r}",
        "{odcs:>9}",
        "{ odcs }",
        "{ODCS}",
        "{",
        "}",
        "a { b",
        "a } b",
        "{odcs",
        "{{odcs}",
        "{nope}",
        "{details}",  # a body placeholder is not allowed in a subject
    ],
)
def test_a_subject_with_anything_but_an_allow_listed_placeholder_is_refused(
    template: str,
) -> None:
    with pytest.raises(TemplateError):
        validate_template(template, "subject")


def test_doubled_braces_are_literals_and_placeholders_substitute() -> None:
    template = "[{{x}}] {odcs} {semver} }}"
    assert validate_template(template, "subject") == template
    assert render_template(template, {"odcs": "orders", "semver": "1.2.0"}, "subject") == (
        "[{x}] orders 1.2.0 }"
    )


def test_rendering_never_evaluates_a_value_that_looks_like_a_template() -> None:
    out = render_template("{odcs}", {"odcs": "{semver}{0.__class__}"}, "subject")
    assert out == "{semver}{0.__class__}"


def test_template_bounds_and_control_characters() -> None:
    with pytest.raises(TemplateError):
        validate_template("", "subject")
    with pytest.raises(TemplateError):
        validate_template("x" * 201, "subject")
    validate_template("x" * 200, "subject")
    with pytest.raises(TemplateError):
        validate_template("a\nb", "subject")
    with pytest.raises(TemplateError):
        validate_template("a\x00b", "subject")
    with pytest.raises(TemplateError):
        validate_template("   ", "subject")
    with pytest.raises(TemplateError):
        validate_template("", "body")
    with pytest.raises(TemplateError):
        validate_template("x" * 8001, "body")
    validate_template("x" * 8000, "body")
    with pytest.raises(TemplateError):
        validate_template("a\tb", "body")
    with pytest.raises(TemplateError):
        validate_template("a\rb", "body")
    assert validate_template("a\r\nb\nc", "body") == "a\nb\nc"


@pytest.mark.parametrize("host", ["hooks.example.com", "*.example.com", "sink", "a-b.example.org"])
def test_host_patterns_accepted(host: str) -> None:
    assert check_host_pattern(host) == host


@pytest.mark.parametrize(
    "host",
    [
        "10.0.0.1",
        "example.com:443",
        "https://x",
        "Example.com",
        "*.",
        "*",
        "",
        "a..b",
        "x_y.com",
        "[::1]",
        "127.0.0.1",
        "*.10.0.0.1",
        "ex ample.com",
        "m\u00fcnchen.de",
        "a" * 64 + ".com",
    ],
)
def test_host_patterns_refused(host: str) -> None:
    with pytest.raises(ValueError):
        check_host_pattern(host)


def test_an_empty_allow_list_allows_no_host() -> None:
    """Quick 261120-s0p (item 6): the list is mandatory, so an empty one allows nothing."""
    assert not webhook_host_allowed("anything.example", [])
    assert not webhook_host_allowed("hooks.example.com", [])


def test_host_matching_only_narrows() -> None:
    patterns = ["hooks.example.com", "*.example.org"]
    assert webhook_host_allowed("hooks.example.com", patterns)
    assert webhook_host_allowed("HOOKS.Example.com", patterns)
    assert webhook_host_allowed("a.b.example.org", patterns)
    assert not webhook_host_allowed("example.org", patterns)
    assert not webhook_host_allowed("evilexample.org", patterns)
    assert not webhook_host_allowed("hooks.example.com.evil.net", patterns)
    assert not webhook_host_allowed("other.example.com", patterns)
    assert webhook_host_allowed("m\u00fcnchen.example.org", patterns)


@pytest.mark.parametrize("address", ["contractum@localhost", "a.b+c@mail.example.com"])
def test_sender_addresses_accepted(address: str) -> None:
    assert check_email_address(address) == address


@pytest.mark.parametrize(
    "address",
    [
        "",
        "a",
        "a@",
        "@b.com",
        "a@@b.com",
        "a b@c.com",
        "<a@b.com>",
        "a@b.com,c@d.com",
        "a@b.com;",
        "a@b:25",
        '"a"@b.com',
        "a\\@b.com",
        "a@b.com\n",
        "a@-b.com",
        "a@b_c.com",
        ".a@b.com",
        "a..b@c.com",
        "a(b)@c.com",
        "\u00e9@b.com",
        "a@" + "x" * 250 + ".com",
    ],
)
def test_sender_addresses_refused(address: str) -> None:
    with pytest.raises(ValueError):
        check_email_address(address)


def test_the_sender_name_follows_the_one_name_rule() -> None:
    value = normalise_section("notifications", {"sender": {"name": "  Équipe données  "}})
    assert value["sender"] == {"name": "Équipe données"}
    with pytest.raises(ValidationError) as caught:
        normalise_section("notifications", {"sender": {"name": "Team\u202e"}})
    (error,) = caught.value.errors()
    assert error["loc"] == ("sender", "name")
    assert error["msg"].startswith("Contains U+202E, which is not allowed.")
    with pytest.raises(ValidationError):
        normalise_section("notifications", {"sender": {"name": "Ops \U0001f600"}})


def test_the_notifications_model_bounds_and_normalisation() -> None:
    value = normalise_section(
        "notifications",
        {
            "retry_schedule_s": [10, 86400],
            "max_attempts": 20,
            "sunset_lead_days": [30, 7, 1],
            "webhook_allowed_hosts": ["b.example.com", "a.example.com", "b.example.com"],
            "sender": {"name": "  Contractum  ", "address": "no-reply@example.com"},
            "email_templates": {
                "contract.version.published": {"subject": "{card}: {odcs}"},
                "generic": {},
            },
        },
    )
    assert value["sunset_lead_days"] == [1, 7, 30]
    assert value["webhook_allowed_hosts"] == ["b.example.com", "a.example.com"]
    assert value["sender"] == {"name": "Contractum", "address": "no-reply@example.com"}
    assert value["email_templates"] == {"contract.version.published": {"subject": "{card}: {odcs}"}}
    assert normalise_section("notifications", {"webhook_allowed_hosts": []}) == {}
    assert normalise_section("notifications", {"email_templates": {}}) == {}


@pytest.mark.parametrize(
    "raw",
    [
        {"retry_schedule_s": []},
        {"retry_schedule_s": [9]},
        {"retry_schedule_s": [86401]},
        {"retry_schedule_s": [60] * 11},
        {"max_attempts": 0},
        {"max_attempts": 21},
        {"sunset_lead_days": []},
        {"sunset_lead_days": [0]},
        {"sunset_lead_days": [366]},
        {"sunset_lead_days": [7, 7]},
        {"sunset_lead_days": [1, 2, 3, 4, 5, 6]},
        {"webhook_allowed_hosts": ["x.example.com"] * 101},
        {"webhook_allowed_hosts": ["10.0.0.1"]},
        {"email_templates": {"nope": {"subject": "x"}}},
        {"email_templates": {"generic": {"subject": "{bogus}"}}},
        {"email_templates": {"generic": {"body": "{odcs.__class__}"}}},
        {"sender": {"name": "a\nb"}},
        {"sender": {"name": "x" * 101}},
        {"sender": {"address": "Name <a@b.com>"}},
        {"sender": {"from": "a@b.com"}},
        {"unknown": 1},
    ],
)
def test_the_notifications_model_refuses(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        NotificationsSettings.model_validate(raw)


def test_a_bad_placeholder_points_at_the_field() -> None:
    with pytest.raises(ValidationError) as caught:
        normalise_section("notifications", {"email_templates": {"generic": {"subject": "{bogus}"}}})
    assert tuple(caught.value.errors()[0]["loc"]) == ("email_templates", "generic", "subject")


def test_import_runs_the_same_notification_checks() -> None:
    document = {
        "format": EXPORT_FORMAT,
        "format_version": 1,
        "sections": {"notifications": {"webhook_allowed_hosts": ["10.0.0.1"]}},
    }
    with pytest.raises(SettingsImportError) as caught:
        parse_export(document)
    assert caught.value.pointer == "/sections/notifications/webhook_allowed_hosts"


def test_the_defaults_view_carries_every_event_type_and_the_builtin_values() -> None:
    view = notifications_defaults()
    assert set(view["email_templates"]) == set(EVENT_TYPES)
    assert view["max_attempts"] == 7
    assert view["retry_schedule_s"] == [60, 300, 1800, 7200, 21600, 43200]
    assert view["sunset_lead_days"] == [7, 30]
    assert view["webhook_allowed_hosts"] == []


# --- governance, security and retention (quick 261120-0ye) -----------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {"mandatory_warn_days": -1},
        {"mandatory_warn_days": 366},
        {"default_odcs_api_version": "3.2.0"},
        {"default_odcs_api_version": "v3.2"},
        {"default_odcs_api_version": "v3.2.0 "},
        {"require_lifecycle_reason": "yes please"},
        {"surprise": True},
    ],
)
def test_the_governance_model_refuses(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        GovernanceSettings.model_validate(raw)


def test_the_governance_model_accepts_its_bounds() -> None:
    assert GovernanceSettings.model_validate({"mandatory_warn_days": 0}).mandatory_warn_days == 0
    ok = {"mandatory_warn_days": 365, "default_odcs_api_version": "v3.1.0"}
    assert normalise_section("governance", ok) == ok


@pytest.mark.parametrize(
    "raw",
    [
        {"credential_max_ttl_days": 0},
        {"credential_max_ttl_days": 3651},
        {"session_max_s": 3599},
        {"session_max_s": 7_776_001},
        {"idle_timeout_s": 899},
        {"idle_timeout_s": 604_801},
        {"lockout_account_threshold": 2},
        {"lockout_account_threshold": 21},
        {"lockout_ip_threshold": 9},
        {"lockout_ip_threshold": 501},
        {"lockout_max_lock_s": 59},
        {"lockout_max_lock_s": 86_401},
        {"password_min_length": 11},
        {"password_min_length": 129},
        {"local_signin_enabled": "no"},
        {"password_require_upper": 2},
        {"surprise": 1},
    ],
)
def test_the_security_model_refuses_out_of_bounds_values(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SecuritySettings.model_validate(raw)


def test_the_security_model_accepts_every_bound() -> None:
    ok = {
        "credential_max_ttl_days": 3650,
        "session_max_s": 3600,
        "idle_timeout_s": 900,
        "lockout_account_threshold": 20,
        "lockout_ip_threshold": 10,
        "lockout_max_lock_s": 86_400,
        "password_min_length": 128,
        "password_require_symbol": True,
        "local_signin_enabled": False,
    }
    assert normalise_section("security", ok) == ok


def test_idle_above_the_session_length_points_at_the_idle_field() -> None:
    with pytest.raises(ValidationError) as caught:
        SecuritySettings.model_validate({"session_max_s": 3600, "idle_timeout_s": 3601})
    assert tuple(caught.value.errors()[0]["loc"]) == ("idle_timeout_s",)
    assert SecuritySettings.model_validate({"session_max_s": 3600, "idle_timeout_s": 3600})


@pytest.mark.parametrize("days", [-1, 1, 89, 36501])
def test_audit_retention_refuses_the_gap_and_the_extremes(days: int) -> None:
    with pytest.raises(ValidationError):
        PlatformSettings.model_validate({"audit_retention_days": days})


@pytest.mark.parametrize("days", [0, 90, 400, 36500])
def test_audit_retention_accepts_zero_and_ninety_to_the_ceiling(days: int) -> None:
    assert (
        PlatformSettings.model_validate({"audit_retention_days": days}).audit_retention_days == days
    )


def test_import_names_the_governance_field() -> None:
    document = {
        "format": EXPORT_FORMAT,
        "format_version": 1,
        "sections": {"governance": {"mandatory_warn_days": 999}},
    }
    with pytest.raises(SettingsImportError) as caught:
        parse_export(document)
    assert caught.value.pointer == "/sections/governance/mandatory_warn_days"


# --- edges (quick 261121-bvy) --------------------------------------------------------------------


def test_plain_text_refuses_a_lone_surrogate() -> None:
    with pytest.raises(ValueError, match="lone surrogate"):
        check_plain_text("bad\ud800")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("Hello \udc00", "lone surrogate"),
        ("Hello \u202e", "bidirectional control"),
    ],
)
def test_a_template_with_a_surrogate_or_bidi_control_is_refused(text: str, message: str) -> None:
    with pytest.raises(TemplateError, match=message):
        validate_template(text, "subject")


def test_a_template_kind_outside_subject_and_body_is_refused() -> None:
    with pytest.raises(ValueError, match="kind must be"):
        validate_template("Hello", "footer")  # type: ignore[arg-type]


@pytest.mark.parametrize("host", ["a..b.example.com", "x" * 64 + ".example.com"])
def test_a_host_idna_cannot_encode_is_never_allowed(host: str) -> None:
    assert webhook_host_allowed(host, ["*.example.com", host]) is False


def test_explicit_nulls_in_the_notifications_model_mean_unset() -> None:
    model = NotificationsSettings.model_validate(
        {
            "email_templates": None,
            "sender": {"name": None, "address": None},
            "sunset_lead_days": None,
            "webhook_allowed_hosts": None,
        }
    )
    assert model.email_templates is None
    assert model.sunset_lead_days is None
    assert model.webhook_allowed_hosts is None
    assert model.sender is not None
    assert (model.sender.name, model.sender.address) == (None, None)


def test_import_refuses_a_non_string_export_time() -> None:
    with pytest.raises(SettingsImportError) as caught:
        parse_export(_doc(exported_at=1700000000))
    assert caught.value.pointer == "/exported_at"


async def test_client_aclose_without_a_request_and_invalidate_forces_a_reread() -> None:
    clock = Clock()
    idle = _client(clock)
    await idle.aclose()  # never opened an HTTP client: nothing to close

    with respx.mock:
        route = respx.get(URL).mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json={"sections": {"platform": {"v": 3}}}),
            ]
        )
        client = _client(clock)
        assert await client.get("platform", bearer="t") is None
        client.invalidate()  # no waiting for the fallback ttl
        assert await client.get("platform", bearer="t") == {"v": 3}
        assert route.call_count == 2
        await client.aclose()


@respx.mock
async def test_client_treats_a_non_object_section_as_unavailable() -> None:
    respx.get(URL).respond(200, json={"sections": {"platform": ["not", "an", "object"]}})
    client = _client(Clock())
    assert await client.get("platform", bearer="t") is None
    await client.aclose()
