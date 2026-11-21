"""``notices.handle_payload`` answers that need no database: events with no audience, events too
malformed to notify anyone (``InvalidNoticeEvent``, acknowledged once) and the empty materialise.

The session factory hands out a session that fails any query, so each case also proves that the
answer was reached before the database was asked.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from dc_contracts.notices import InvalidNoticeEvent, handle_payload, materialise, notice_payload
from dc_contracts.settings import ContractsSettings
from dc_core.events import subject_for

pytestmark = pytest.mark.unit

DB = "postgresql+psycopg://nobody:nobody@127.0.0.1:1/never"
SETTINGS = ContractsSettings(database_url=DB)  # type: ignore[arg-type]
CARD = str(uuid.uuid4())
CONTRACT = str(uuid.uuid4())
VERSION = str(uuid.uuid4())


class _NoQuerySession:
    async def __aenter__(self) -> _NoQuerySession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def begin(self) -> _NoQuerySession:
        return self

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("no query expected")


def _event(
    action: str,
    after: dict[str, Any] | None,
    *,
    aggregate_type: str = "contract",
    aggregate_id: str = CONTRACT,
    before: dict[str, Any] | None = None,
) -> tuple[bytes, str]:
    envelope = {
        "event_id": str(uuid.uuid4()),
        "occurred_at": datetime(2026, 11, 21, 9, 0, tzinfo=UTC).isoformat(),
        "service": "contracts",
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "action": action,
        "before": before,
        "after": after,
    }
    return json.dumps(envelope).encode(), subject_for("contracts", aggregate_type, action)


async def _handle(payload: bytes, subject: str) -> int:
    return await handle_payload(_NoQuerySession, payload, subject=subject, settings=SETTINGS)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("action", "after"),
    [
        # Written before notices existed: no audience, no pin snapshot.
        ("contract.version_published", {"id": VERSION, "semver": "2.0.0"}),
        ("contract.version_promoted", {"version": {"id": VERSION}}),
        # A publish whose only pin is on the new version itself has nothing to compare.
        (
            "contract.version_published",
            {"id": VERSION, "notify_pins": [{"card_id": CARD, "version_id": VERSION}]},
        ),
        # A refused unpublish that names no pinned card.
        ("contract.version_unpublish_blocked", {"version_id": VERSION, "notify_card_ids": []}),
    ],
)
async def test_an_event_without_an_audience_records_nothing(
    action: str, after: dict[str, Any]
) -> None:
    assert await _handle(*_event(action, after)) == 0


@pytest.mark.parametrize(
    ("action", "after", "kwargs", "message"),
    [
        (
            "contract.version_deprecated",
            {"id": VERSION, "notify_card_ids": [CARD]},
            {"aggregate_id": "not-a-uuid"},
            "aggregate is not a contract id",
        ),
        (
            "contract.version_promoted",
            {"version": "2.0.0", "notify_card_ids": [CARD]},
            {},
            "does not carry its version",
        ),
        (
            "card.subscription_removed",
            {"version_id": VERSION},
            {"aggregate_type": "card", "aggregate_id": CARD},
            "does not name its card, contract and version",
        ),
        (
            "contract.version_published",
            {"id": VERSION, "notify_pins": ["not a pin"]},
            {},
            "a pin in the publish event is malformed",
        ),
        (
            "contract.version_unpublish_blocked",
            {"notify_card_ids": [CARD]},
            {},
            "does not name its version",
        ),
    ],
)
async def test_a_malformed_event_is_invalid_and_never_retried(
    action: str, after: dict[str, Any], kwargs: dict[str, str], message: str
) -> None:
    with pytest.raises(InvalidNoticeEvent, match=message):
        await _handle(*_event(action, after, **kwargs))


async def test_an_envelope_whose_action_is_not_a_subject_token_is_invalid() -> None:
    payload, subject = _event("contract.version_published", {})
    broken = json.loads(payload)
    broken["action"] = "contract.version published"
    with pytest.raises(InvalidNoticeEvent, match="does not name a valid subject"):
        await _handle(json.dumps(broken).encode(), subject)


async def test_materialising_for_no_card_writes_nothing() -> None:
    created = await materialise(
        _NoQuerySession(),  # type: ignore[arg-type]
        card_ids=[],
        event_type="contract.version.published",
        contract_id=CONTRACT,
        version_id=VERSION,
        payload={},
        dedupe_key="event:x",
        occurred_at=datetime.now(UTC),
    )
    assert created == 0


def test_a_payload_without_a_version_carries_only_the_contract() -> None:
    assert notice_payload(contract_id=CONTRACT, odcs_id="urn:x", version_id=None, semver=None) == {
        "contract": {"id": CONTRACT, "odcs_id": "urn:x"}
    }
