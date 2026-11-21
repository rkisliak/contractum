"""SUB-07 / D-105 / T-04.1-31: the email a notice becomes (subject templates, header safety, body).

Every rendered message must validate as the mail service's own request model, so a header-injection
attempt through a contract id can never reach SMTP.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from dc_contracts.mailer import (
    EMAIL_SUBJECTS,
    render_email,
    sanitize_header_text,
)
from dc_contracts.notices import NOTICE_TYPES
from dc_core.mail import MailSendIn

pytestmark = pytest.mark.unit

ODCS_ID = "urn:datacontract:orders"


@dataclass
class FakeNotice:
    event_type: str
    payload: dict[str, Any]
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    card_id: uuid.UUID = field(default_factory=uuid.uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime(2026, 11, 13, 9, 30, tzinfo=UTC))


def payload(odcs_id: str = ODCS_ID, **extra: Any) -> dict[str, Any]:
    contract_id = str(uuid.uuid4())
    base: dict[str, Any] = {
        "contract": {"id": contract_id, "odcs_id": odcs_id},
        "version": {"id": str(uuid.uuid4()), "semver": "2.3.0"},
    }
    base.update(extra)
    return base


EXTRAS: dict[str, dict[str, Any]] = {
    "contract.version.published": {"status": "preview"},
    "contract.version.breaking_published": {"pinned_versions": []},
    "contract.latest.changed": {"previous_latest": {"id": str(uuid.uuid4()), "semver": "2.2.0"}},
    "contract.version.deprecated": {"sunset_date": "2027-01-12", "deprecated_at": "2026-11-13"},
    "contract.version.sunset_approaching": {"sunset_date": "2027-01-12", "lead_days": 14},
    "contract.version.unpublish_blocked": {},
    "card.subscription.removed": {"reason": "duplicate pin", "removed_by": "someone"},
}


def test_every_notice_type_has_a_subject_template() -> None:
    assert set(EMAIL_SUBJECTS) == set(NOTICE_TYPES)


def test_a_deprecation_subject_names_the_contract_version_and_sunset() -> None:
    notice = FakeNotice(
        "contract.version.deprecated", payload(**EXTRAS["contract.version.deprecated"])
    )
    subject, text = render_email(notice, "orders consumers")
    assert subject == f"[Contractum] {ODCS_ID} 2.3.0 deprecated (sunset 2027-01-12)"
    assert "orders consumers" in text
    assert ODCS_ID in text
    assert "2.3.0" in text
    assert "2027-01-12" in text
    assert str(notice.id) in text
    assert notice.payload["contract"]["id"] in text


def test_the_body_carries_no_members_or_document_text() -> None:
    notice = FakeNotice(
        "contract.version.deprecated",
        payload(**EXTRAS["contract.version.deprecated"], document="SECRET-DOC", members=["g1"]),
    )
    _, text = render_email(notice, "orders consumers")
    assert "members" not in text.lower()
    assert "SECRET-DOC" not in text
    assert "g1" not in text


def test_control_characters_in_the_odcs_id_never_reach_the_subject() -> None:
    notice = FakeNotice(
        "contract.version.deprecated",
        payload(odcs_id="urn:x\r\nBcc: x@y", **EXTRAS["contract.version.deprecated"]),
    )
    subject, text = render_email(notice, "orders consumers")
    assert "\r" not in subject
    assert "\n" not in subject
    MailSendIn(to=["a@example.test"], subject=subject, text=text)


def test_a_long_odcs_id_is_cut_to_eighty_characters_in_the_subject() -> None:
    notice = FakeNotice(
        "contract.version.deprecated",
        payload(odcs_id="u" * 300, **EXTRAS["contract.version.deprecated"]),
    )
    subject, _ = render_email(notice, "orders consumers")
    assert "u" * 80 in subject
    assert "u" * 81 not in subject


def test_sanitize_header_text_strips_controls_collapses_whitespace_and_truncates() -> None:
    assert sanitize_header_text("a\r\nb\x00c\x7fd", 80) == "a bcd"
    assert sanitize_header_text("  a \t  b  ", 80) == "a b"
    assert sanitize_header_text("x" * 100, 10) == "x" * 10


@pytest.mark.parametrize("event_type", NOTICE_TYPES)
def test_every_rendered_message_validates_as_a_mail_request(event_type: str) -> None:
    notice = FakeNotice(event_type, payload(**EXTRAS[event_type]))
    subject, text = render_email(notice, "orders consumers")
    assert subject.startswith("[Contractum] ")
    MailSendIn(to=["a@example.test"], subject=subject, text=text)


@pytest.mark.parametrize("event_type", NOTICE_TYPES)
def test_a_hostile_card_name_and_fields_still_validate(event_type: str) -> None:
    extras = {key: "v\r\nBcc: x@y\x00" + "z" * 600 for key in ("reason", "sunset_date", "status")}
    notice = FakeNotice(event_type, payload(odcs_id="o\r\n" + "o" * 400, **extras))
    subject, text = render_email(notice, "n\r\nBcc: x@y\x1b" + "n" * 400)
    MailSendIn(to=["a@example.test"], subject=subject, text=text)


# --- the breaking-change notice (SUB-08, D-164, T-6-37) ----------------------------------------


def breaking_payload(sections: list[dict[str, Any]]) -> dict[str, Any]:
    return payload(
        version={"id": str(uuid.uuid4()), "semver": "2.0.0"},
        classifier_version="odcs-compat-1",
        truncated_pins=0,
        pinned_versions=sections,
    )


def section(
    semver: str, changes: list[dict[str, Any]], *, total: int | None = None
) -> dict[str, Any]:
    return {
        "version_id": str(uuid.uuid4()),
        "semver": semver,
        "breaking_count": total if total is not None else len(changes),
        "changes_total": total if total is not None else len(changes),
        "changes": changes,
        "diff_url": f"/api/v1/contracts/contracts/c1/diff?from={semver}&to=2.0.0",
    }


def removed(path: str) -> dict[str, Any]:
    return {"code": "property-removed", "path": path, "attribute": None}


def test_a_breaking_notice_lists_each_pinned_version_and_its_changes() -> None:
    tightened = {
        "code": "constraint-tightened",
        "path": "schema.orders.properties.id",
        "attribute": "maxLength",
        "before": 10,
        "after": 5,
    }
    notice = FakeNotice(
        "contract.version.breaking_published",
        breaking_payload(
            [
                section("1.0.0", [removed("schema.orders.properties.amount"), tightened]),
                section("1.9.0", [removed("schema.orders.properties.note")]),
            ]
        ),
    )
    subject, text = render_email(notice, "orders consumers")
    assert subject == f"[Contractum] {ODCS_ID} 2.0.0 published with breaking changes"
    assert "Pinned version 1.0.0: 2 breaking change(s)" in text
    assert "  property-removed schema.orders.properties.amount" in text
    assert "  constraint-tightened schema.orders.properties.id maxLength 10 -> 5" in text
    assert text.index("Pinned version 1.0.0") < text.index("Pinned version 1.9.0")
    MailSendIn(to=["a@example.test"], subject=subject, text=text)


def test_a_hostile_property_name_is_one_capped_line() -> None:
    evil = removed("schema.orders.properties.a\r\nBcc: x@y\x00" + "p" * 600)
    notice = FakeNotice(
        "contract.version.breaking_published", breaking_payload([section("1.0.0", [evil])])
    )
    subject, text = render_email(notice, "orders consumers")
    (line,) = [entry for entry in text.split("\n") if "property-removed" in entry]
    assert len(line.strip()) <= 300
    assert "Bcc: x@y" in line  # kept as inert text on the same line, never a header
    assert "\x00" not in text
    MailSendIn(to=["a@example.test"], subject=subject, text=text)


def test_at_most_fifty_change_lines_and_a_more_line_with_the_diff_url() -> None:
    changes = [removed(f"schema.orders.properties.c{i}") for i in range(50)]
    notice = FakeNotice(
        "contract.version.breaking_published",
        breaking_payload([section("1.0.0", changes, total=60)]),
    )
    subject, text = render_email(notice, "orders consumers")
    assert text.count("property-removed") == 50
    assert "  and 10 more, see /api/v1/contracts/contracts/c1/diff?from=1.0.0&to=2.0.0" in text
    MailSendIn(to=["a@example.test"], subject=subject, text=text)


def test_the_fifty_line_limit_is_for_the_whole_message_not_each_version() -> None:
    sections = [
        section(f"1.{i}.0", [removed(f"schema.orders.properties.v{i}c{j}") for j in range(30)])
        for i in range(3)
    ]
    notice = FakeNotice("contract.version.breaking_published", breaking_payload(sections))
    _, text = render_email(notice, "orders consumers")
    assert text.count("property-removed") == 50
    assert "  and 10 more, see" in text


def test_the_worst_case_message_still_fits_the_mail_service() -> None:
    long_path = "schema.orders.properties." + "x" * 600
    sections = [
        section(f"1.{i}.0", [removed(long_path) for _ in range(50)], total=500) for i in range(25)
    ]
    notice = FakeNotice("contract.version.breaking_published", breaking_payload(sections))
    subject, text = render_email(notice, "n" * 500)
    MailSendIn(to=["a@example.test"], subject=subject, text=text)
    assert text.count("property-removed") <= 50


def test_a_malformed_breaking_payload_renders_without_a_list() -> None:
    notice = FakeNotice(
        "contract.version.breaking_published", payload(pinned_versions="not a list")
    )
    subject, text = render_email(notice, "orders consumers")
    assert "Breaking changes" not in text
    MailSendIn(to=["a@example.test"], subject=subject, text=text)


def test_other_event_types_get_no_breaking_block() -> None:
    notice = FakeNotice(
        "contract.version.published",
        payload(status="preview", pinned_versions=[section("1.0.0", [removed("a.b")])]),
    )
    _, text = render_email(notice, "orders consumers")
    assert "property-removed" not in text
    assert "Breaking changes" not in text


# --- quick 261120-0yd: stored templates ----------------------------------------------------------


def _deprecated(**extra: Any) -> FakeNotice:
    return FakeNotice(
        "contract.version.deprecated", payload(**EXTRAS["contract.version.deprecated"], **extra)
    )


def test_a_stored_subject_and_body_replace_the_defaults() -> None:
    templates = {
        "contract.version.deprecated": {
            "subject": "{card}: {odcs} {semver} goes away on {sunset_date}",
            "body": "Hello {card}.\n{details}\nSee {contract_id} / {notice_id} at {occurred_at}.",
        }
    }
    notice = _deprecated()
    subject, text = render_email(notice, "Analytics team", templates)
    assert subject == f"Analytics team: {ODCS_ID} 2.3.0 goes away on 2027-01-12"
    assert text.startswith("Hello Analytics team.\nCard: Analytics team\n")
    assert notice.payload["contract"]["id"] in text
    assert str(notice.id) in text
    assert "You get this message" not in text
    MailSendIn(to=["a@example.test"], subject=subject, text=text)


def test_a_stored_subject_alone_keeps_the_default_body() -> None:
    notice = _deprecated()
    default_subject, default_text = render_email(notice, "c")
    subject, text = render_email(notice, "c", {notice.event_type: {"subject": "custom {odcs}"}})
    assert subject == f"custom {ODCS_ID}"
    assert text == default_text.replace(default_subject, subject, 1)


def test_the_generic_template_applies_to_an_unknown_event_type() -> None:
    notice = FakeNotice("contract.something.else", payload())
    subject, _ = render_email(notice, "c", {"generic": {"subject": "other {event}"}})
    assert subject == "other contract.something.else"


def test_a_value_that_tries_to_inject_a_header_stays_one_line_in_a_stored_subject() -> None:
    notice = _deprecated(card_hint="x")
    notice.payload["reason"] = "ok\r\nBcc: evil@example.test"
    templates = {notice.event_type: {"subject": "Reason: {reason}"}}
    subject, text = render_email(notice, "card\r\nBcc: evil2@example.test", templates)
    assert "\r" not in subject
    assert "\n" not in subject
    assert subject == "Reason: ok Bcc: evil@example.test"
    MailSendIn(to=["a@example.test"], subject=subject, text=text)
    subject2, _ = render_email(
        notice, "card\r\nBcc: evil2@example.test", {notice.event_type: {"subject": "{card}"}}
    )
    assert "\n" not in subject2


def test_a_placeholder_value_that_looks_like_a_template_is_not_evaluated() -> None:
    notice = _deprecated()
    subject, _ = render_email(
        notice, "{semver}{0.__class__}", {notice.event_type: {"subject": "[{card}]"}}
    )
    assert subject == "[{semver}{0.__class__}]"


def test_a_stored_template_that_no_longer_validates_falls_back_to_the_default() -> None:
    notice = _deprecated()
    default = render_email(notice, "c")
    broken = {notice.event_type: {"subject": "{odcs.__class__}", "body": "{nope}"}}
    assert render_email(notice, "c", broken) == default


def test_a_rendered_subject_is_cut_to_the_mail_services_cap() -> None:
    notice = _deprecated()
    templates = {notice.event_type: {"subject": "{odcs} {odcs} {odcs} {card} {card}"}}
    subject, text = render_email(notice, "c" * 300, templates)
    assert len(subject) <= 200
    MailSendIn(to=["a@example.test"], subject=subject, text=text)


def test_the_rendered_body_is_cut_to_the_mail_services_text_cap() -> None:
    notice = _deprecated()
    templates = {notice.event_type: {"body": "{details}\n" * 400}}
    _, text = render_email(notice, "c", templates)
    assert len(text) <= 20000


def test_a_list_value_renders_as_a_short_bracketed_list_of_scalars() -> None:
    narrowed = {
        "code": "enum-narrowed",
        "path": "schema.orders.properties.state",
        "attribute": "enum",
        "before": ["open", "closed", {"nested": "never"}],
        "after": ["open"],
    }
    notice = FakeNotice(
        "contract.version.breaking_published", breaking_payload([section("1.0.0", [narrowed])])
    )
    _, text = render_email(notice, "orders consumers")
    assert "  enum-narrowed schema.orders.properties.state enum [open, closed, -] -> [open]" in text
    assert "nested" not in text


def test_a_section_that_is_not_a_mapping_is_left_out() -> None:
    sections: list[Any] = ["not a section", section("1.0.0", [removed("a.b")])]
    notice = FakeNotice("contract.version.breaking_published", breaking_payload(sections))
    _, text = render_email(notice, "orders consumers")
    assert text.count("Pinned version") == 1
    assert "not a section" not in text
