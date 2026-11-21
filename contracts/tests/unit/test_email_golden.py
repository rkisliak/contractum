# ruff: noqa: E501 - the expected texts are literal strings, kept on single lines on purpose.
"""Golden emails (quick 261120-0yd, D-20): with no stored template every notice email is
byte-identical to what ``render_email`` produced before templates became editable.

The expected texts below were captured from the pre-refactor renderer for a fixed notice (fixed
ids and time) and are asserted for every event type, the breaking-change email with overflow
included. Do not regenerate them to make a change pass: a difference is a regression.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from dc_contracts.mailer import render_email

pytestmark = pytest.mark.unit

NID = uuid.UUID("00000000-0000-4000-8000-000000000001")
CID = uuid.UUID("00000000-0000-4000-8000-000000000002")
CONTRACT = "00000000-0000-4000-8000-000000000003"
VERSION = "00000000-0000-4000-8000-000000000004"
WHEN = datetime(2026, 11, 13, 9, 30, tzinfo=UTC)


@dataclass
class N:
    event_type: str
    payload: dict[str, Any]
    id: uuid.UUID
    card_id: uuid.UUID
    occurred_at: datetime


def base(**extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "contract": {"id": CONTRACT, "odcs_id": "urn:datacontract:orders"},
        "version": {"id": VERSION, "semver": "2.3.0"},
    }
    d.update(extra)
    return d


def chg(i: int) -> dict[str, Any]:
    return {
        "code": "property-removed",
        "path": f"/schema/0/properties/{i}",
        "attribute": "name",
        "before": f"col{i}",
        "after": None,
    }


CASES: dict[str, tuple[str, dict[str, Any]]] = {
    "published": ("contract.version.published", base(status="preview")),
    "breaking": (
        "contract.version.breaking_published",
        base(
            pinned_versions=[
                {
                    "semver": "1.0.0",
                    "breaking_count": 60,
                    "changes_total": 60,
                    "diff_url": "/api/v1/contracts/contracts/x/diff?from=1.0.0&to=2.3.0",
                    "changes": [chg(i) for i in range(60)],
                },
                {
                    "semver": "1.1.0",
                    "breaking_count": 2,
                    "changes_total": 2,
                    "changes": [chg(100), chg(101)],
                },
            ]
        ),
    ),
    "breaking_small": (
        "contract.version.breaking_published",
        base(
            pinned_versions=[
                {
                    "semver": "2.0.0",
                    "breaking_count": 2,
                    "changes_total": 2,
                    "changes": [chg(1), chg(2)],
                }
            ]
        ),
    ),
    "breaking_empty": ("contract.version.breaking_published", base(pinned_versions=[])),
    "latest": (
        "contract.latest.changed",
        base(previous_latest={"id": VERSION, "semver": "2.2.0"}),
    ),
    "deprecated": (
        "contract.version.deprecated",
        base(sunset_date="2027-01-12", deprecated_at="2026-11-13", reason="replaced by 3.0.0"),
    ),
    "sunset": (
        "contract.version.sunset_approaching",
        base(sunset_date="2027-01-12", lead_days=14),
    ),
    "unpublish": ("contract.version.unpublish_blocked", base()),
    "removed": ("card.subscription.removed", base(reason="duplicate pin", removed_by="someone")),
    "generic": ("contract.something.else", base()),
    "hostile": (
        "contract.version.deprecated",
        {
            "contract": {"id": CONTRACT, "odcs_id": "urn:x\r\nBcc: x@y"},
            "version": {"id": VERSION, "semver": "2.3.0\n"},
            "sunset_date": "2027-01-12",
        },
    ),
}

GOLDEN: dict[str, tuple[str, str]] = {
    "published": (
        "[Contractum] urn:datacontract:orders 2.3.0 published",
        "[Contractum] urn:datacontract:orders 2.3.0 published\n\nCard: Analytics team\nEvent: contract.version.published\nContract id: 00000000-0000-4000-8000-000000000003\nODCS id: urn:datacontract:orders\nVersion: 2.3.0\nStatus: preview\nNotice id: 00000000-0000-4000-8000-000000000001\nOccurred at: 2026-11-13T09:30:00+00:00\n\nYou get this message because your subscriber card has the email channel on.\n",
    ),
    "breaking": (
        "[Contractum] urn:datacontract:orders 2.3.0 published with breaking changes",
        "[Contractum] urn:datacontract:orders 2.3.0 published with breaking changes\n\nCard: Analytics team\nEvent: contract.version.breaking_published\nContract id: 00000000-0000-4000-8000-000000000003\nODCS id: urn:datacontract:orders\nVersion: 2.3.0\nNotice id: 00000000-0000-4000-8000-000000000001\nOccurred at: 2026-11-13T09:30:00+00:00\n\nBreaking changes for the versions your card pins:\nPinned version 1.0.0: 60 breaking change(s)\n  property-removed /schema/0/properties/0 name col0 -> -\n  property-removed /schema/0/properties/1 name col1 -> -\n  property-removed /schema/0/properties/2 name col2 -> -\n  property-removed /schema/0/properties/3 name col3 -> -\n  property-removed /schema/0/properties/4 name col4 -> -\n  property-removed /schema/0/properties/5 name col5 -> -\n  property-removed /schema/0/properties/6 name col6 -> -\n  property-removed /schema/0/properties/7 name col7 -> -\n  property-removed /schema/0/properties/8 name col8 -> -\n  property-removed /schema/0/properties/9 name col9 -> -\n  property-removed /schema/0/properties/10 name col10 -> -\n  property-removed /schema/0/properties/11 name col11 -> -\n  property-removed /schema/0/properties/12 name col12 -> -\n  property-removed /schema/0/properties/13 name col13 -> -\n  property-removed /schema/0/properties/14 name col14 -> -\n  property-removed /schema/0/properties/15 name col15 -> -\n  property-removed /schema/0/properties/16 name col16 -> -\n  property-removed /schema/0/properties/17 name col17 -> -\n  property-removed /schema/0/properties/18 name col18 -> -\n  property-removed /schema/0/properties/19 name col19 -> -\n  property-removed /schema/0/properties/20 name col20 -> -\n  property-removed /schema/0/properties/21 name col21 -> -\n  property-removed /schema/0/properties/22 name col22 -> -\n  property-removed /schema/0/properties/23 name col23 -> -\n  property-removed /schema/0/properties/24 name col24 -> -\n  property-removed /schema/0/properties/25 name col25 -> -\n  property-removed /schema/0/properties/26 name col26 -> -\n  property-removed /schema/0/properties/27 name col27 -> -\n  property-removed /schema/0/properties/28 name col28 -> -\n  property-removed /schema/0/properties/29 name col29 -> -\n  property-removed /schema/0/properties/30 name col30 -> -\n  property-removed /schema/0/properties/31 name col31 -> -\n  property-removed /schema/0/properties/32 name col32 -> -\n  property-removed /schema/0/properties/33 name col33 -> -\n  property-removed /schema/0/properties/34 name col34 -> -\n  property-removed /schema/0/properties/35 name col35 -> -\n  property-removed /schema/0/properties/36 name col36 -> -\n  property-removed /schema/0/properties/37 name col37 -> -\n  property-removed /schema/0/properties/38 name col38 -> -\n  property-removed /schema/0/properties/39 name col39 -> -\n  property-removed /schema/0/properties/40 name col40 -> -\n  property-removed /schema/0/properties/41 name col41 -> -\n  property-removed /schema/0/properties/42 name col42 -> -\n  property-removed /schema/0/properties/43 name col43 -> -\n  property-removed /schema/0/properties/44 name col44 -> -\n  property-removed /schema/0/properties/45 name col45 -> -\n  property-removed /schema/0/properties/46 name col46 -> -\n  property-removed /schema/0/properties/47 name col47 -> -\n  property-removed /schema/0/properties/48 name col48 -> -\n  property-removed /schema/0/properties/49 name col49 -> -\n  and 10 more, see /api/v1/contracts/contracts/x/diff?from=1.0.0&to=2.3.0\nPinned version 1.1.0: 2 breaking change(s)\n  and 2 more\n\nYou get this message because your subscriber card has the email channel on.\n",
    ),
    "breaking_small": (
        "[Contractum] urn:datacontract:orders 2.3.0 published with breaking changes",
        "[Contractum] urn:datacontract:orders 2.3.0 published with breaking changes\n\nCard: Analytics team\nEvent: contract.version.breaking_published\nContract id: 00000000-0000-4000-8000-000000000003\nODCS id: urn:datacontract:orders\nVersion: 2.3.0\nNotice id: 00000000-0000-4000-8000-000000000001\nOccurred at: 2026-11-13T09:30:00+00:00\n\nBreaking changes for the versions your card pins:\nPinned version 2.0.0: 2 breaking change(s)\n  property-removed /schema/0/properties/1 name col1 -> -\n  property-removed /schema/0/properties/2 name col2 -> -\n\nYou get this message because your subscriber card has the email channel on.\n",
    ),
    "breaking_empty": (
        "[Contractum] urn:datacontract:orders 2.3.0 published with breaking changes",
        "[Contractum] urn:datacontract:orders 2.3.0 published with breaking changes\n\nCard: Analytics team\nEvent: contract.version.breaking_published\nContract id: 00000000-0000-4000-8000-000000000003\nODCS id: urn:datacontract:orders\nVersion: 2.3.0\nNotice id: 00000000-0000-4000-8000-000000000001\nOccurred at: 2026-11-13T09:30:00+00:00\n\nYou get this message because your subscriber card has the email channel on.\n",
    ),
    "latest": (
        "[Contractum] urn:datacontract:orders 2.3.0 is now the Latest version",
        "[Contractum] urn:datacontract:orders 2.3.0 is now the Latest version\n\nCard: Analytics team\nEvent: contract.latest.changed\nContract id: 00000000-0000-4000-8000-000000000003\nODCS id: urn:datacontract:orders\nVersion: 2.3.0\nPrevious Latest version: 2.2.0\nNotice id: 00000000-0000-4000-8000-000000000001\nOccurred at: 2026-11-13T09:30:00+00:00\n\nYou get this message because your subscriber card has the email channel on.\n",
    ),
    "deprecated": (
        "[Contractum] urn:datacontract:orders 2.3.0 deprecated (sunset 2027-01-12)",
        "[Contractum] urn:datacontract:orders 2.3.0 deprecated (sunset 2027-01-12)\n\nCard: Analytics team\nEvent: contract.version.deprecated\nContract id: 00000000-0000-4000-8000-000000000003\nODCS id: urn:datacontract:orders\nVersion: 2.3.0\nSunset date: 2027-01-12\nDeprecated at: 2026-11-13\nReason: replaced by 3.0.0\nNotice id: 00000000-0000-4000-8000-000000000001\nOccurred at: 2026-11-13T09:30:00+00:00\n\nYou get this message because your subscriber card has the email channel on.\n",
    ),
    "sunset": (
        "[Contractum] urn:datacontract:orders 2.3.0 sunset approaching (2027-01-12)",
        "[Contractum] urn:datacontract:orders 2.3.0 sunset approaching (2027-01-12)\n\nCard: Analytics team\nEvent: contract.version.sunset_approaching\nContract id: 00000000-0000-4000-8000-000000000003\nODCS id: urn:datacontract:orders\nVersion: 2.3.0\nSunset date: 2027-01-12\nDays until sunset: 14\nNotice id: 00000000-0000-4000-8000-000000000001\nOccurred at: 2026-11-13T09:30:00+00:00\n\nYou get this message because your subscriber card has the email channel on.\n",
    ),
    "unpublish": (
        "[Contractum] urn:datacontract:orders 2.3.0: the owner wants to unpublish it",
        "[Contractum] urn:datacontract:orders 2.3.0: the owner wants to unpublish it\n\nCard: Analytics team\nEvent: contract.version.unpublish_blocked\nContract id: 00000000-0000-4000-8000-000000000003\nODCS id: urn:datacontract:orders\nVersion: 2.3.0\nNotice id: 00000000-0000-4000-8000-000000000001\nOccurred at: 2026-11-13T09:30:00+00:00\n\nYou get this message because your subscriber card has the email channel on.\n",
    ),
    "removed": (
        "[Contractum] your pin of urn:datacontract:orders 2.3.0 was removed",
        "[Contractum] your pin of urn:datacontract:orders 2.3.0 was removed\n\nCard: Analytics team\nEvent: card.subscription.removed\nContract id: 00000000-0000-4000-8000-000000000003\nODCS id: urn:datacontract:orders\nVersion: 2.3.0\nReason: duplicate pin\nNotice id: 00000000-0000-4000-8000-000000000001\nOccurred at: 2026-11-13T09:30:00+00:00\n\nYou get this message because your subscriber card has the email channel on.\n",
    ),
    "generic": (
        "[Contractum] notice for urn:datacontract:orders",
        "[Contractum] notice for urn:datacontract:orders\n\nCard: Analytics team\nEvent: contract.something.else\nContract id: 00000000-0000-4000-8000-000000000003\nODCS id: urn:datacontract:orders\nVersion: 2.3.0\nNotice id: 00000000-0000-4000-8000-000000000001\nOccurred at: 2026-11-13T09:30:00+00:00\n\nYou get this message because your subscriber card has the email channel on.\n",
    ),
    "hostile": (
        "[Contractum] urn:x Bcc: x@y 2.3.0 deprecated (sunset 2027-01-12)",
        "[Contractum] urn:x Bcc: x@y 2.3.0 deprecated (sunset 2027-01-12)\n\nCard: Analytics team\nEvent: contract.version.deprecated\nContract id: 00000000-0000-4000-8000-000000000003\nODCS id: urn:x Bcc: x@y\nVersion: 2.3.0\nSunset date: 2027-01-12\nNotice id: 00000000-0000-4000-8000-000000000001\nOccurred at: 2026-11-13T09:30:00+00:00\n\nYou get this message because your subscriber card has the email channel on.\n",
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_email_is_byte_identical_to_the_pre_template_renderer(case: str) -> None:
    event_type, payload = CASES[case]
    notice = N(event_type, payload, NID, CID, WHEN)
    assert render_email(notice, "Analytics team") == GOLDEN[case]


def test_every_case_has_a_golden_text() -> None:
    assert set(GOLDEN) == set(CASES)
